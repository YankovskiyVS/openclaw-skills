"""Classify and deduplicate local export payloads without downloading anything."""

import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from claude_import.models import InputFile, Settings, digest_file, encode, private_json
from claude_import.source import json_values, unpack


def hint_for(name: str) -> str:
    """Recognize export category names only as a fallback for empty payloads."""
    aliases: dict[str, str] = {
        "design_chats": "design_chats",
        "conversations": "conversations",
        "memories": "memories",
        "memory_files": "memories",
        "projects": "projects",
        "artifacts": "frames",
        "frames": "frames",
        "reflections": "feedback",
        "feedback": "feedback",
        "login_history": "light_metadata",
        "users": "light_metadata",
    }
    for token, category in aliases.items():
        if token in name.lower():
            return category
    return "unknown"


def classify(value: JsonValue) -> str:
    """Detect supported Claude records from their structural fields."""
    if not isinstance(value, dict):
        return "unknown"
    if isinstance(value.get("data_files"), list):
        return "manifest"
    if "uuid" in value and isinstance(value.get("chat_messages"), list):
        return "conversations"
    if (
        "uuid" in value
        and isinstance(value.get("messages"), list)
        and "created_at" in value
    ):
        return "design_chats"
    if "uuid" in value and isinstance(value.get("docs"), list):
        return "projects"
    if any(
        key in value
        for key in ("conversations_memory", "project_memories", "memory_files")
    ):
        return "memories"
    if isinstance(value.get("path"), str) and isinstance(value.get("content"), str):
        return "memory-file"
    if "id" in value and isinstance(value.get("versions"), list):
        return "frames"
    if isinstance(value.get("reflections"), list):
        return "feedback"
    if isinstance(value.get("login_events"), list) or "email_address" in value:
        return "light_metadata"
    if isinstance(value.get("comments"), list):
        return "artifact-comment"
    return "unknown"


@dataclass
class Asset:
    """One extracted or directly supplied local file."""

    path: Path
    origin: str
    hint: str


class LocalInputs:
    """Preserve original bytes and normalize a mixed set into declared category folders."""

    def __init__(self, work: Path, settings: Settings) -> None:
        """Create staging owned by the current import operation."""
        self.work: Path = work
        self.settings: Settings = settings
        self.assets: list[Asset] = []
        self.inputs: list[InputFile] = []
        self.archives: set[str] = set()
        self.outputs: set[str] = set()
        self.frame_roots: set[Path] = set()
        self.expanded_bytes: int = 0
        self.duplicates: int = 0
        self.unknown: int = 0
        self.manifests: int = 0
        (work / "originals").mkdir(mode=0o700, exist_ok=True)
        (work / "source").mkdir(mode=0o700, exist_ok=True)

    def expand(self, path: Path, origin: str, hint: str, depth: int = 0) -> None:
        """Recursively open local ZIPs while enforcing an aggregate expansion limit."""
        if path.suffix.lower() != ".zip" and not zipfile.is_zipfile(path):
            self.assets.append(
                Asset(
                    path,
                    origin,
                    hint_for(origin) if hint_for(origin) != "unknown" else hint,
                )
            )
            return
        fingerprint: str = digest_file(path)
        if fingerprint in self.archives:
            self.duplicates += 1
            return
        if depth >= self.settings.max_archive_depth:
            raise ValueError("Nested ZIP depth exceeds CLAUDE_IMPORT_MAX_ARCHIVE_DEPTH")
        with zipfile.ZipFile(path) as archive:
            self.expanded_bytes += sum(item.file_size for item in archive.infolist())
        if self.expanded_bytes > self.settings.max_unpacked_bytes:
            raise ValueError(
                "Combined ZIP expansion exceeds CLAUDE_IMPORT_MAX_UNPACKED_BYTES"
            )
        target: Path = self.work / "extracted" / fingerprint
        target.mkdir(parents=True, mode=0o700)
        unpack(path, target, self.settings)
        self.archives.add(fingerprint)
        for extracted in sorted(target.rglob("*")):
            if extracted.is_file() and not any(
                part in {"__MACOSX", ".DS_Store"} for part in extracted.parts
            ):
                relative: str = str(extracted.relative_to(target))
                self.expand(extracted, origin + "/" + relative, hint, depth + 1)

    def store(self, category: str, value: JsonValue, origin: str) -> None:
        """Publish one canonical source record, retaining distinct source snapshots."""
        if category == "manifest":
            self.manifests += 1
            return
        if category == "memory-file":
            value = {"memory_files": [value]}
            category = "memories"
        elif category == "artifact-comment":
            value = [value]
            category = "frames"
        payload: JsonValue = (
            [value]
            if category == "conversations" and isinstance(value, dict)
            else value
        )
        serialized: str = encode(payload)
        fingerprint: str = hashlib.sha256(serialized.encode()).hexdigest()
        key: str = category + "/" + fingerprint
        if key in self.outputs:
            self.duplicates += 1
            return
        self.outputs.add(key)
        if category == "unknown":
            self.unknown += 1
        target: Path = (
            self.work / "source" / (category + "-000") / (fingerprint + ".json")
        )
        private_json(target, payload)

    def record(self, value: JsonValue, asset: Asset) -> None:
        """Normalize ordinary records and known multi-category export envelopes."""
        category: str = classify(value)
        if category == "unknown" and isinstance(value, dict):
            wrappers: list[str] = [
                key
                for key in ("conversations", "projects", "design_chats", "memories")
                if isinstance(value.get(key), list)
            ]
            if wrappers:
                for key in wrappers:
                    rows: JsonValue = value[key]
                    assert isinstance(rows, list)
                    for row in rows:
                        self.record(row, asset)
                return
        if category == "frames":
            self.frame_roots.add(asset.path.parent)
        self.store(category, value, asset.origin)

    def read_json(self, asset: Asset) -> None:
        """Stream large conversation arrays and classify each record by its schema."""
        with asset.path.open(encoding="utf-8-sig") as stream:
            prefix: str = stream.read(4096).lstrip()
        if (
            prefix.startswith("[")
            and asset.hint == "frames"
            and "comments" in asset.path.name.lower()
        ):
            self.store("frames", json.loads(asset.path.read_bytes()), asset.origin)
        elif prefix.startswith("["):
            count: int = 0
            for value in json_values(asset.path):
                self.record(value, asset)
                count += 1
            if not count:
                self.store("unknown", [], asset.origin)
        else:
            value: JsonValue = json.loads(asset.path.read_bytes())
            self.record(value, asset)

    def companion(self, asset: Asset) -> None:
        """Preserve inert artifact files and archive unsupported companions."""
        category: str = (
            "frames"
            if asset.hint == "frames"
            or any(
                root == asset.path.parent or root in asset.path.parents
                for root in self.frame_roots
            )
            else "unknown"
        )
        fingerprint: str = digest_file(asset.path)
        # Artifact versions with different names remain separate source versions.
        name: str = re.sub(r"[^a-zA-Z0-9_.-]", "_", asset.path.name)
        artifact_scope: str = next(
            (
                part
                for index, part in enumerate(Path(asset.origin).parts)
                if index and Path(asset.origin).parts[index - 1] == "artifacts"
            ),
            "unscoped",
        )
        fingerprint = hashlib.sha256(
            (artifact_scope + "/" + fingerprint).encode()
        ).hexdigest()
        key: str = category + "/" + name + "/" + fingerprint
        if key in self.outputs:
            self.duplicates += 1
            return
        self.outputs.add(key)
        if category == "unknown":
            self.unknown += 1
        target: Path = self.work / "source" / (category + "-000") / fingerprint / name
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        shutil.copyfile(asset.path, target)
        target.chmod(0o600)

    def collect(self, files: list[Path]) -> None:
        """Process the complete provided set without interpreting manifest URLs."""
        for path in files:
            fingerprint: str = digest_file(path)
            target: Path = self.work / "originals" / fingerprint
            if not target.exists():
                shutil.copyfile(path, target)
                target.chmod(0o600)
            if digest_file(target) != fingerprint:
                raise ValueError("Input changed while being preserved")
            kind: str = (
                "archive"
                if path.suffix.lower() == ".zip" or zipfile.is_zipfile(path)
                else "local_file"
            )
            self.inputs.append(
                InputFile(filename=path.name, digest=fingerprint, category=kind)
            )
            self.expand(path, str(path), hint_for(str(path)))
        for asset in self.assets:
            if asset.path.suffix.lower() == ".json":
                self.read_json(asset)
        for asset in self.assets:
            if asset.path.suffix.lower() != ".json":
                self.companion(asset)
