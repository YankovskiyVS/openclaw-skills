"""Apply prepared exports to a running Gateway and verify before deleting staging."""

import json
import shutil
from pathlib import Path

from pydantic import JsonValue

from claude_import.memory import publish_guide, verify_guide
from claude_import.models import Plan, PreparedChat, Receipt, digest_file, private_json
from claude_import.runtime import Runtime
from claude_import.source import tree_digest


def load_plan(work: Path) -> Plan:
    """Load a plan only from a matching owned operation directory."""
    owner: dict[str, JsonValue] = json.loads(
        (work / ".claude-import-owner.json").read_bytes()
    )
    plan: Plan = Plan.model_validate_json((work / "plan.json").read_bytes())
    if (
        owner.get("batch_id") != plan.batch_id
        or owner.get("importer") != "claude-history-v2"
    ):
        raise ValueError("Work directory ownership does not match input")
    return plan


def load_receipt(work: Path) -> Receipt:
    """Read progress and check it belongs to the prepared input."""
    plan: Plan = load_plan(work)
    receipt: Receipt = Receipt.model_validate_json((work / "receipt.json").read_bytes())
    if receipt.batch_id != plan.batch_id:
        raise ValueError("Receipt does not match plan")
    return receipt


def save_receipt(work: Path, receipt: Receipt) -> None:
    """Commit resumable progress locally and in the permanent source archive."""
    private_json(work / "receipt.json", receipt.model_dump(mode="json"))
    if receipt.archive_dir:
        private_json(
            receipt.archive_dir / "receipt.json", receipt.model_dump(mode="json")
        )


def preserve(work: Path, plan: Plan, archive: Path) -> None:
    """Publish verified original and prepared files without overwriting prior material."""
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, digest in [
        ("originals", plan.originals_digest),
        ("source", plan.source_digest),
        ("prepared", plan.prepared_digest),
        ("references", plan.reference_digest),
    ]:
        target: Path = archive / name
        if not target.exists():
            pending: Path = archive / (name + ".pending")
            if pending.exists():
                shutil.rmtree(pending)
            shutil.copytree(work / name, pending)
            if tree_digest(pending) != digest:
                raise ValueError("Source archive copy failed verification: " + name)
            pending.replace(target)
        if tree_digest(target) != digest:
            raise ValueError("Existing permanent archive conflicts: " + name)
    private_json(archive / "plan.json", plan.model_dump(mode="json"))


def publish_references(plan: Plan, archive: Path, workspace: Path) -> int:
    """Add immutable reference files inside the selected agent workspace only."""
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    root: Path = workspace.resolve(strict=True)
    destination: Path = root / "memory" / "imports" / "claude-web"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not destination.resolve().is_relative_to(root):
        raise ValueError("Memory import destination escapes selected workspace")
    for reference in plan.references:
        target: Path = destination / reference.path
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.parent.resolve().is_relative_to(root) or target.is_symlink():
            raise ValueError("Reference destination is unsafe")
        if target.exists():
            if digest_file(target) != reference.digest:
                raise ValueError(
                    "An existing reference was changed; it was not overwritten"
                )
            continue
        original: Path = archive / "references" / reference.path
        with original.open("rb") as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
        target.chmod(0o600)
        if digest_file(target) != reference.digest:
            raise ValueError("Published reference checksum mismatch")
    return len(plan.references)


def chat_file(chat: PreparedChat) -> str:
    """Locate a small immutable chat descriptor within the permanent archive."""
    return "prepared/chats/" + chat.session_key.rsplit(":", 1)[-1] + "/chat.json"


def verify(work: Path, runtime: Runtime, receipt: Receipt | None = None) -> Receipt:
    """Verify every native message/tree and every retained source/reference file."""
    plan: Plan = load_plan(work)
    receipt = receipt or load_receipt(work)
    if not receipt.archive_dir or not receipt.workspace:
        raise ValueError("No applied import to verify")
    status: dict[str, JsonValue] = runtime.call("status", {"agentId": plan.agent})
    if status.get("version") != "2026.7.1" or status.get("workspace") != str(
        receipt.workspace
    ):
        raise ValueError("Destination runtime or workspace changed")
    for chat in plan.chats:
        result: dict[str, JsonValue] = runtime.call(
            "verify",
            {
                "agentId": plan.agent,
                "archive": str(receipt.archive_dir),
                "chatFile": chat_file(chat),
            },
        )
        if (
            result.get("sessionId") != receipt.sessions.get(chat.session_key)
            or result.get("verified") is not True
        ):
            raise ValueError("Native session verification failed")
    listing: dict[str, JsonValue] = runtime.call("list", {"agentId": plan.agent})
    keys: JsonValue = listing.get("keys")
    if not isinstance(keys, list) or not set(
        chat.session_key for chat in plan.chats
    ).issubset(set(str(key) for key in keys)):
        raise ValueError("Not all imported sessions are present in the native SDK list")
    for reference in plan.references:
        target: Path = receipt.workspace / "memory/imports/claude-web" / reference.path
        if not target.is_file() or digest_file(target) != reference.digest:
            raise ValueError("Imported reference is missing or changed")
    verify_guide(receipt.workspace, receipt.memory_guide_status)
    for name, expected in (
        ("originals", plan.originals_digest),
        ("source", plan.source_digest),
        ("prepared", plan.prepared_digest),
        ("references", plan.reference_digest),
    ):
        if tree_digest(receipt.archive_dir / name) != expected:
            raise ValueError("Permanent archive is missing or changed: " + name)
    receipt.status = "cleaned" if receipt.status == "cleaned" else "verified"
    receipt.verified_chats = [chat.session_key for chat in plan.chats]
    save_receipt(work, receipt)
    return receipt


def apply(work: Path, runtime: Runtime, imports_dir: Path | None = None) -> Receipt:
    """Publish complete new transcripts before atomically registering their identities."""
    plan: Plan = load_plan(work)
    receipt: Receipt = load_receipt(work)
    status: dict[str, JsonValue] = runtime.call("status", {"agentId": plan.agent})
    if (
        status.get("protocol") != 2
        or status.get("version") != "2026.7.1"
        or not isinstance(status.get("workspace"), str)
    ):
        raise ValueError("Unsupported destination runtime")
    if receipt.status in {"verified", "cleaned"}:
        return verify(work, runtime, receipt)
    if imports_dir is None:
        if not isinstance(status.get("stateDir"), str):
            raise ValueError("SDK did not resolve the active state directory")
        imports_dir = Path(status["stateDir"]) / "imports" / "claude-v2"
    archive: Path = imports_dir.resolve() / plan.batch_id
    if archive == work or archive.is_relative_to(work) or work.is_relative_to(archive):
        raise ValueError("Permanent archive and temporary work must not overlap")
    if receipt.archive_dir and receipt.archive_dir != archive:
        raise ValueError("Permanent archive location changed")
    preserve(work, plan, archive)
    receipt.archive_dir = archive
    receipt.workspace = Path(status["workspace"]).resolve()
    receipt.status = "applying"
    save_receipt(work, receipt)
    receipt.reference_files = publish_references(plan, archive, receipt.workspace)
    budget: JsonValue = status.get("bootstrapMaxChars", 20000)
    receipt.memory_guide_status = publish_guide(
        plan,
        receipt.workspace,
        int(budget) if isinstance(budget, (int, float)) else 20000,
    )
    save_receipt(work, receipt)
    for index, chat in enumerate(plan.chats):
        result: dict[str, JsonValue] = runtime.call(
            "publish",
            {
                "agentId": plan.agent,
                "archive": str(archive),
                "chatFile": chat_file(chat),
            },
        )
        if result.get("verified") is not True or not isinstance(
            result.get("sessionId"), str
        ):
            raise ValueError("SDK did not publish a verified native session")
        session_id: str = result["sessionId"]
        if (
            chat.session_key in receipt.sessions
            and receipt.sessions[chat.session_key] != session_id
        ):
            raise ValueError("Previously imported session was replaced")
        receipt.sessions[chat.session_key] = session_id
        if chat.session_key not in receipt.verified_chats:
            receipt.verified_chats.append(chat.session_key)
        save_receipt(work, receipt)
        if index % 10 == 0 or index + 1 == len(plan.chats):
            print(f"Verified native chat {index + 1}/{len(plan.chats)}", flush=True)
    return verify(work, runtime, receipt)


def cleanup(work: Path, runtime: Runtime) -> Receipt:
    """Remove owned staging only after a fresh full verification."""
    receipt: Receipt = load_receipt(work)
    if receipt.status not in {"verified", "cleaned"}:
        raise ValueError("Cleanup requires a verified import")
    verify(work, runtime, receipt)
    for name in ("originals", "extracted", "source", "prepared", "references"):
        directory: Path = work / name
        if directory.exists():
            shutil.rmtree(directory)
    for filename in (
        "merge.sqlite",
        "merge.sqlite-journal",
        "merge.sqlite-wal",
        "merge.sqlite-shm",
    ):
        (work / filename).unlink(missing_ok=True)
    receipt.status = "cleaned"
    save_receipt(work, receipt)
    return receipt
