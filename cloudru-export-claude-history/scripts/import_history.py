"""Import a complete Claude export into a running OpenClaw without model replay."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from pydantic import ValidationError

from claude_import.convert import normalize
from claude_import.inputs import LocalInputs
from claude_import.migration import apply, cleanup, load_plan, load_receipt, verify
from claude_import.models import Plan, Receipt, Settings, private_json
from claude_import.runtime import Runtime
from claude_import.source import batch_id, discover_files

SKILL_VERSION: str = "2.1.3"


def prepare(
    source: Path | list[Path], work: Path, agent: str, settings: Settings
) -> Plan:
    """Normalize only supplied local files into a resumable import."""
    work = work.resolve()
    paths: list[Path] = discover_files(
        [source] if isinstance(source, Path) else source, work
    )
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", agent):
        raise ValueError("Invalid destination agent")
    batch: str = batch_id(paths)
    marker: Path = work / ".claude-import-owner.json"
    if work.exists() and not marker.is_file():
        raise ValueError(
            "Choose a new private work directory; existing content is not owned by this importer"
        )
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    if marker.exists():
        owner: dict[str, object] = json.loads(marker.read_bytes())
        if (
            owner.get("batch_id") != batch
            or owner.get("importer") != "claude-history-v2"
        ):
            raise ValueError("Work directory belongs to another export")
    else:
        private_json(marker, {"batch_id": batch, "importer": "claude-history-v2"})
    if (work / "plan.json").is_file():
        plan: Plan = load_plan(work)
        if plan.agent != agent:
            raise ValueError("Prepared input belongs to another destination agent")
        return plan
    for name in ("prepared", "references", "source", "originals", "extracted"):
        if (work / name).exists():
            shutil.rmtree(work / name)
    (work / "merge.sqlite").unlink(missing_ok=True)
    inputs: LocalInputs = LocalInputs(work, settings)
    inputs.collect(paths)
    plan = normalize(work, batch, agent, len(inputs.archives), inputs.inputs)
    plan.statistics.update(
        {
            "duplicate_payloads": inputs.duplicates,
            "unsupported_records": inputs.unknown,
            "optional_manifests": inputs.manifests,
            "input_files": len(inputs.inputs),
        }
    )
    if inputs.unknown:
        plan.notices.append(
            "Неизвестные данные сохранены в исходниках без преобразования: "
            + str(inputs.unknown)
        )
    if not plan.chats:
        plan.notices.append(
            "В переданных файлах нет диалогов; сохранены только доступные материалы и память."
        )
    private_json(work / "plan.json", plan.model_dump(mode="json"))
    private_json(work / "receipt.json", Receipt(batch_id=batch).model_dump(mode="json"))
    return plan


def summary(plan: Plan, receipt: Receipt | None = None) -> dict[str, object]:
    """Describe progress without printing conversation text or private links."""
    return {
        "batch_id": plan.batch_id,
        "status": receipt.status if receipt else "prepared",
        "archives": plan.archives,
        "chats": len(plan.chats),
        "messages": sum(chat.messages for chat in plan.chats),
        "reference_files": len(plan.references),
        "statistics": plan.statistics,
        "memory_guide_status": receipt.memory_guide_status if receipt else "pending",
        "archive_dir": str(receipt.archive_dir)
        if receipt and receipt.archive_dir
        else None,
        "runtime_version": plan.runtime_version,
        "history_destination": "native_sessions",
        "notices": plan.notices,
    }


def main() -> int:
    """Run preparation, authorized live apply, verification and scoped cleanup."""
    os.umask(0o077)
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "prepare",
            "apply",
            "verify",
            "cleanup",
            "status",
            "run",
            "check",
        ],
    )
    parser.add_argument("--source", type=Path, action="extend", nargs="+")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--imports-dir", type=Path)
    parser.add_argument("--agent", default="main")
    parser.add_argument("--env-file", type=Path)
    arguments: argparse.Namespace = parser.parse_args()
    try:
        settings: Settings = Settings(_env_file=arguments.env_file)
        if arguments.state_dir:
            os.environ["OPENCLAW_STATE_DIR"] = str(arguments.state_dir.resolve())
            os.environ["OPENCLAW_CONFIG_PATH"] = str(
                arguments.state_dir.resolve() / "openclaw.json"
            )
        imports_dir: Path | None = arguments.imports_dir
        if arguments.action == "check":
            with Runtime(settings, Path(os.devnull)) as runtime:
                print(json.dumps(runtime.call("status", {"agentId": arguments.agent})))
            return 0
        if not arguments.work:
            parser.error("--work is required")
        work: Path = arguments.work.resolve()
        if arguments.action == "run":
            with Runtime(settings, Path(os.devnull)) as runtime:
                runtime.call("status", {"agentId": arguments.agent})
        if arguments.action in {"prepare", "run"}:
            if not arguments.source:
                parser.error("--source is required")
            plan: Plan = prepare(arguments.source, work, arguments.agent, settings)
        else:
            plan = load_plan(work)
        receipt: Receipt = load_receipt(work)
        if arguments.action not in {"prepare", "status"}:
            with Runtime(settings, work / "sdk-client.log") as runtime:
                if arguments.action in {"apply", "run"}:
                    receipt = apply(work, runtime, imports_dir)
                elif arguments.action == "verify":
                    receipt = verify(work, runtime)
                if arguments.action in {"cleanup", "run"}:
                    receipt = cleanup(work, runtime)
        print(json.dumps(summary(plan, receipt), ensure_ascii=False))
        return 0
    except ValidationError as error:
        locations: list[str] = [
            ".".join(str(part) for part in item["loc"])
            for item in error.errors(include_input=False)
        ]
        print(
            "Unsupported source/configuration fields: " + ", ".join(locations),
            file=sys.stderr,
        )
        return 1
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
