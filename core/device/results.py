"""设备能力层的返回值类型。

`CommandResult` 与 `core.services.speaker.CommandResult` 字段一致，保证
`SpeakerManager.run_shell()` 的对外返回形状不变（ticket 02 是 prefactor）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommandResult:
    """一次设备 shell/ubus 调用的原始结果。"""

    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True)
class CapabilityResult:
    """一次能力调用的「请求结果 + 回读结果」。

    `ok` 按能力表登记的成功判据计算：
      - `json_code` / `json_parsed` / `json_field`：看解析后的 JSON；
      - `exit_code`：看退出码（成功时无响应体的方法）；
      - `stdout_contains`：看固定输出；
      - `readback`：写操作只看回读（返回码不构成失败判据）。

    `readback` 是回读值（例如 `{"mic": "on"}`、`{"volume": 35}`），
    `readback_ok` 表示回读是否符合期望；上层据此判定写操作是否真正生效。
    """

    capability: str
    ok: bool
    request: CommandResult | None
    readback: dict[str, Any] | None = None
    readback_ok: bool | None = None
    error: str | None = None
    exception: str | None = None

    @property
    def stdout(self) -> str:
        return self.request.stdout if self.request is not None else ""

    @property
    def stderr(self) -> str:
        return self.request.stderr if self.request is not None else ""

    @property
    def exit_code(self) -> int | None:
        return self.request.exit_code if self.request is not None else None

    @property
    def code(self) -> int | None:
        """解析 stdout 里的 JSON `code` 字段（无则 None）。"""

        from core.utils.base import json_decode

        data = json_decode(self.stdout)
        if not isinstance(data, dict):
            return None
        code = data.get("code")
        return code if isinstance(code, int) else None
