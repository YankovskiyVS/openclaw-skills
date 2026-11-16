"""Validated local input settings and resumable import plans."""

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, JsonValue
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read local input limits and the installed OpenClaw runtime settings."""

    model_config = SettingsConfigDict(env_prefix="CLAUDE_IMPORT_", extra="ignore")
    max_unpacked_bytes: int = Field(default=100 * 1024**3, ge=1)
    max_archive_depth: int = Field(default=3, ge=1, le=10)
    openclaw_bin: str = Field(default="openclaw", min_length=1)
    openclaw_package_dir: Path | None = None
    node_bin: str = Field(default="node", min_length=1)
    rpc_timeout: int = Field(default=60000, ge=1000, le=300000)


class InputFile(BaseModel):
    """One immutable local input payload and its container format."""

    filename: str
    digest: str
    category: str


class Reference(BaseModel):
    """An immutable, searchable reference file relative to the imported namespace."""

    path: str
    title: str = ""
    kind: str
    digest: str
    source_project: str | None = None


class PreparedChat(BaseModel):
    """A native chat identity and exact message/tree verification facts."""

    source_id: str
    kind: Literal["conversation", "design"]
    session_key: str
    title: str
    category: str
    messages: int = Field(ge=0)
    digest: str
    chunks: list[str]
    leaves: list[str]
    active_leaf: str | None
    created_ms: int = Field(default=0, ge=0)


class Plan(BaseModel):
    """Small import inventory; message bodies live in bounded batch files."""

    protocol: Literal[2] = 2
    runtime_version: Literal["2026.7.1"] = "2026.7.1"
    batch_id: str
    agent: str
    archives: int = Field(ge=0)
    inputs: list[InputFile] = Field(default_factory=list)
    originals_digest: str
    source_digest: str
    prepared_digest: str
    reference_digest: str
    chats: list[PreparedChat]
    references: list[Reference]
    statistics: dict[str, int]
    notices: list[str]


class Receipt(BaseModel):
    """Persist operation progress without conversation text or credentials."""

    batch_id: str
    status: Literal["prepared", "applying", "verified", "cleaned"] = "prepared"
    archive_dir: Path | None = None
    workspace: Path | None = None
    runtime_version: Literal["2026.7.1"] = "2026.7.1"
    sessions: dict[str, str] = Field(default_factory=dict)
    verified_chats: list[str] = Field(default_factory=list)
    reference_files: int = 0
    memory_guide_status: str = "not_needed"


def encode(value: JsonValue) -> str:
    """Encode stable Unicode JSON without truncation."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_file(path: Path) -> str:
    """Hash file bytes with bounded memory."""
    digest = hashlib.sha256()
    chunk: bytes
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def private_json(path: Path, value: JsonValue) -> None:
    """Atomically publish a private JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path = path.with_suffix(path.suffix + ".tmp")
    descriptor: int = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(encode(value) + "\n")
    temporary.replace(path)
