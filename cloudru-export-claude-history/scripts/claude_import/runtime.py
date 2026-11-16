"""Use the installed 2026.7.1 SDK without a Gateway plugin or auth-token discovery."""

import json
import os
import re
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import TextIO

from pydantic import JsonValue

from claude_import.models import Settings, encode

VERSION: str = "2026.7.1"


def discover_package(settings: Settings) -> Path:
    """Resolve the installed runtime and reject unsupported storage versions."""
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [settings.openclaw_bin, "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    if not re.search(r"\b2026\.7\.1(?:\s|$)", result.stdout):
        raise ValueError(
            "This v2 skill requires OpenClaw 2026.7.1; no writes performed"
        )
    candidates: list[Path] = []
    if settings.openclaw_package_dir:
        candidates.append(settings.openclaw_package_dir)
    else:
        executable: str | None = shutil.which(settings.openclaw_bin)
        if executable:
            resolved: Path = Path(executable).resolve()
            candidates.extend(resolved.parents)
        candidates.append(
            Path.home() / ".openclaw/runtime/current/lib/node_modules/openclaw"
        )
        if shutil.which("npm"):
            global_root: subprocess.CompletedProcess[str] = subprocess.run(
                ["npm", "root", "--global"],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            candidates.append(Path(global_root.stdout.strip()) / "openclaw")
    for candidate in candidates:
        metadata: Path = candidate / "package.json"
        if not metadata.is_file():
            continue
        package: JsonValue = json.loads(metadata.read_bytes())
        if isinstance(package, dict) and package.get("name") == "openclaw":
            if package.get("version") != VERSION:
                raise ValueError(
                    "CLI and SDK package versions differ; no writes performed"
                )
            return candidate.resolve(strict=True)
    raise ValueError(
        "Cannot locate installed SDK; set CLAUDE_IMPORT_OPENCLAW_PACKAGE_DIR"
    )


class Runtime:
    """Exchange bounded requests with one local SDK process using private pipes."""

    def __init__(self, settings: Settings, log: Path) -> None:
        """Start only a migration helper; no Gateway processes are managed."""
        self.settings: Settings = settings
        self.package: Path = discover_package(settings)
        self.sequence: int = 0
        self.closed: bool = False
        self.buffer: bytearray = bytearray()
        self.log: TextIO = log.open("a", encoding="utf-8")
        helper: Path = Path(__file__).resolve().parents[1] / "openclaw_legacy.mjs"
        self.process: subprocess.Popen[bytes] = subprocess.Popen(
            [settings.node_bin, str(helper), str(self.package)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            bufsize=0,
        )
        if self.process.stdout is None:
            raise RuntimeError("SDK helper stdout is unavailable")
        os.set_blocking(self.process.stdout.fileno(), False)
        self.selector: selectors.BaseSelector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def call(self, action: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Send one operation while keeping message bodies out of model context."""
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("SDK helper pipes are unavailable")
        self.sequence += 1
        request: dict[str, JsonValue] = {
            "id": self.sequence,
            "params": params | {"action": action},
        }
        self.process.stdin.write((encode(request) + "\n").encode())
        self.process.stdin.flush()
        deadline: float = time.monotonic() + self.settings.rpc_timeout / 1000 + 30
        while True:
            if b"\n" not in self.buffer:
                remaining: float = deadline - time.monotonic()
                if remaining <= 0 or not self.selector.select(remaining):
                    self.process.terminate()
                    self.close()
                    raise RuntimeError(
                        "SDK helper timed out; sources and progress retained"
                    )
                chunk: bytes = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError(
                        "SDK helper exited; inspect the private sdk-client.log"
                    )
                self.buffer.extend(chunk)
                if len(self.buffer) > 16 * 1024 * 1024:
                    raise ValueError("SDK response exceeds 16 MiB")
                continue
            line, _, remainder = self.buffer.partition(b"\n")
            self.buffer = bytearray(remainder)
            try:
                response: JsonValue = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(response, dict)
                or response.get("protocol") != 2
                or response.get("id") != self.sequence
            ):
                continue
            if response.get("ok") is not True:
                raise RuntimeError(str(response.get("error", "SDK import failed")))
            result: JsonValue = response.get("result")
            if not isinstance(result, dict):
                raise ValueError("Unexpected SDK response")
            return result

    def close(self) -> None:
        """Close the helper without touching the Gateway."""
        if self.closed:
            return
        self.closed = True
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
        if self.process.stdout:
            self.process.stdout.close()
        self.selector.close()
        self.log.close()

    def __enter__(self) -> "Runtime":
        """Return the active helper."""
        return self

    def __exit__(
        self, exception_type: object, exception: object, traceback: object
    ) -> None:
        """Release this helper after completion or failure."""
        self.close()
