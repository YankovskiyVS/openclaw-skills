"""Read local Claude exports with safe ZIP extraction and streaming JSON."""

import hashlib
import json
import shutil
import stat
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from pydantic import JsonValue

from claude_import.models import Settings, digest_file


def discover_files(sources: list[Path], work: Path) -> list[Path]:
    """Collect only explicitly provided files and directory descendants."""
    found: set[Path] = set()
    for source in sources:
        if source.is_symlink():
            raise ValueError("Input symbolic links are not accepted")
        source = source.resolve(strict=True)
        if (
            source == work
            or source.is_relative_to(work)
            or (source.is_dir() and work.is_relative_to(source))
        ):
            raise ValueError("Source must be outside the private work directory")
        candidates: list[Path] = (
            list(source.rglob("*")) if source.is_dir() else [source]
        )
        for candidate in candidates:
            if any(
                part in {"__MACOSX", ".DS_Store", ".git", ".venv", "__pycache__"}
                for part in candidate.parts
            ):
                continue
            if candidate.is_symlink():
                raise ValueError("Input symbolic links are not accepted")
            if candidate.is_file():
                found.add(candidate.resolve())
    if not found:
        raise ValueError("No local export files were provided")
    return sorted(found)


def batch_id(paths: list[Path]) -> str:
    """Identify the input bytes independently of upload order or filenames."""
    fingerprints: set[str] = {digest_file(path) for path in paths}
    return hashlib.sha256("\n".join(sorted(fingerprints)).encode()).hexdigest()[:24]


def unpack(archive_path: Path, destination: Path, settings: Settings) -> None:
    """Extract regular ZIP entries after validating every path and expanded size."""
    with zipfile.ZipFile(archive_path) as archive:
        total: int = 0
        for entry in archive.infolist():
            relative: PurePosixPath = PurePosixPath(entry.filename)
            mode: int = entry.external_attr >> 16
            if (
                not relative.parts
                or relative.is_absolute()
                or ".." in relative.parts
                or "\\" in entry.filename
                or ":" in relative.parts[0]
                or stat.S_ISLNK(mode)
            ):
                raise ValueError(f"Unsafe ZIP member in {archive_path.name}")
            total += entry.file_size
            if total > settings.max_unpacked_bytes:
                raise ValueError("Archive exceeds CLAUDE_IMPORT_MAX_UNPACKED_BYTES")
        for entry in archive.infolist():
            target: Path = destination / entry.filename
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.open(entry) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o600)


def json_values(path: Path) -> Iterator[JsonValue]:
    """Stream a top-level JSON array, retaining at most one conversation in memory."""
    decoder: json.JSONDecoder = json.JSONDecoder()
    with path.open(encoding="utf-8-sig") as source:
        buffer: str = ""
        ended: bool = False

        def fill(size: int = 65536) -> None:
            """Append a chunk or mark the stream complete."""
            nonlocal buffer, ended
            chunk: str = source.read(size)
            ended = not chunk
            buffer += chunk

        def skip_space() -> None:
            """Discard whitespace while acquiring the next token."""
            nonlocal buffer
            while True:
                buffer = buffer.lstrip()
                if buffer or ended:
                    return
                fill()

        skip_space()
        if not buffer.startswith("["):
            raise ValueError(f"Expected a JSON conversation array: {path.name}")
        buffer = buffer[1:]
        first: bool = True
        while True:
            skip_space()
            if buffer.startswith("]"):
                buffer = buffer[1:]
                skip_space()
                if buffer:
                    raise ValueError("Unexpected content after JSON array")
                return
            if not first:
                if not buffer.startswith(","):
                    raise ValueError("Expected a comma between conversations")
                buffer = buffer[1:]
                skip_space()
                if buffer.startswith("]"):
                    raise ValueError("Trailing comma in JSON array")
            while True:
                try:
                    value, offset = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    if ended:
                        raise ValueError(
                            f"Invalid or incomplete conversation JSON: {path.name}"
                        ) from None
                    fill(max(65536, len(buffer)))
            yield value
            buffer = buffer[offset:]
            first = False


def json_records(path: Path) -> Iterator[dict[str, JsonValue]]:
    """Stream conversation objects from a top-level JSON array."""
    for value in json_values(path):
        if not isinstance(value, dict):
            raise ValueError("Expected a conversation object")
        yield value


def tree_digest(directory: Path) -> str:
    """Hash relative filenames and bytes of a preserved source directory."""
    if not directory.is_dir():
        raise ValueError("Source directory is missing")
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symbolic links are not accepted in migration sources")
        if path.is_file():
            digest.update(str(path.relative_to(directory)).encode())
            digest.update(bytes.fromhex(digest_file(path)))
    return digest.hexdigest()
