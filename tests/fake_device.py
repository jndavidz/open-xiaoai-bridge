"""Fake device transport for contract-level tests.

The bridge talks to the speaker through a narrow boundary:

    run_shell(script, timeout) -> JSON {stdout, stderr, exit_code}
    on_event(event_json)
    on_input_data(pcm_bytes)

This module fakes that boundary in memory. It never touches a real device,
the network, or the Rust extension. Tests can register ordered command rules,
inject events / audio, and assert what the business layer actually sent.

Keep this file dependency-free (stdlib only) so it runs in the WSL system
Python without installing project dependencies.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable


@dataclass
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0

    def as_json(self) -> str:
        return json.dumps(
            {
                "stdout": self.stdout,
                "stderr": self.stderr,
                "exit_code": self.exit_code,
            },
            ensure_ascii=False,
        )


@dataclass
class _Rule:
    matcher: Callable[[str], bool]
    result: CommandResult
    timeout: bool = False


@dataclass
class FakeDevice:
    """In-memory implementation of the bridge's device boundary."""

    calls: list[tuple[str, float]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    audio_frames: list[bytes] = field(default_factory=list)
    _rules: list[_Rule] = field(default_factory=list)
    _default: CommandResult = field(
        default_factory=lambda: CommandResult("", "no matching fake rule", 127)
    )

    # ---- rule registration ----

    def on(
        self,
        contains: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        timeout: bool = False,
    ) -> "FakeDevice":
        """Register a rule matched by substring (last registered wins)."""
        return self._add(
            lambda script: contains in script,
            CommandResult(stdout, stderr, exit_code),
            timeout,
        )

    def on_glob(
        self,
        pattern: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        timeout: bool = False,
    ) -> "FakeDevice":
        return self._add(
            lambda script: fnmatch.fnmatchcase(script, pattern),
            CommandResult(stdout, stderr, exit_code),
            timeout,
        )

    def on_regex(
        self,
        pattern: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        timeout: bool = False,
    ) -> "FakeDevice":
        compiled = re.compile(pattern)
        return self._add(
            lambda script: bool(compiled.search(script)),
            CommandResult(stdout, stderr, exit_code),
            timeout,
        )

    def on_json(
        self,
        contains: str,
        payload: dict,
        exit_code: int = 0,
    ) -> "FakeDevice":
        return self.on(contains, json.dumps(payload, ensure_ascii=False), exit_code=exit_code)

    def set_default(self, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self._default = CommandResult(stdout, stderr, exit_code)

    def _add(
        self,
        matcher: Callable[[str], bool],
        result: CommandResult,
        timeout: bool,
    ) -> "FakeDevice":
        self._rules.append(_Rule(matcher, result, timeout))
        return self

    # ---- boundary implementation ----

    async def run_shell(self, script: str, timeout: float = 10_000) -> str:
        self.calls.append((script, timeout))
        for rule in reversed(self._rules):
            if rule.matcher(script):
                if rule.timeout:
                    raise asyncio.TimeoutError("fake device timeout")
                return rule.result.as_json()
        return self._default.as_json()

    async def emit_event(self, event: str, data=None) -> None:
        """Record an event payload as the bridge would receive it."""
        payload = {"event": event}
        if data is not None:
            payload["data"] = data
        self.events.append(json.dumps(payload, ensure_ascii=False))

    def feed_audio(self, pcm: bytes) -> None:
        self.audio_frames.append(pcm)

    # ---- assertions ----

    def called(self, contains: str) -> bool:
        return any(contains in script for script, _ in self.calls)

    def matching_calls(self, contains: str) -> list[str]:
        return [script for script, _ in self.calls if contains in script]

    def assert_called(self, contains: str) -> None:
        if not self.called(contains):
            rendered = "\n".join(f"  - {script}" for script, _ in self.calls) or "  (none)"
            raise AssertionError(
                f"expected a device command containing {contains!r}; got:\n{rendered}"
            )

    def assert_not_called(self, contains: str) -> None:
        if self.called(contains):
            raise AssertionError(f"did not expect a device command containing {contains!r}")

    @property
    def last_script(self) -> str:
        if not self.calls:
            raise AssertionError("no device command has been issued")
        return self.calls[-1][0]

    def reset(self) -> None:
        self.calls.clear()
        self.events.clear()
        self.audio_frames.clear()


class FakeXiaoAI:
    """Minimal stand-in for core.xiaoai.XiaoAI, wired to a FakeDevice."""

    def __init__(self, device: FakeDevice):
        self.device = device
        self.input_handler = None

    async def run_shell(self, script: str, timeout: float = 10_000) -> str:
        return await self.device.run_shell(script, timeout)

    def on_input_data(self, data: bytes) -> None:
        self.device.feed_audio(data)
        if self.input_handler is not None:
            self.input_handler(data)


def load_speaker_module():
    """Import core.services.speaker with the Rust extension stubbed out.

    speaker.py imports open_xiaoai_server at module import time. Tests that
    only exercise shell-backed methods do not need the native module, so we
    provide a stub if the real one is unavailable.
    """

    import sys
    import types
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    if "open_xiaoai_server" not in sys.modules:
        sys.modules["open_xiaoai_server"] = types.ModuleType("open_xiaoai_server")

    from core.services import speaker  # noqa: WPS433 (intentional late import)

    return speaker
