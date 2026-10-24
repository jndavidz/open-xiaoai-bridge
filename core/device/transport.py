"""设备 shell 传输层。

能力层唯一的下行边界：`run_shell(script, timeout_ms)` 返回设备侧 JSON
`{stdout, stderr, exit_code}`。默认实现转发给 `XiaoAI.run_shell`（经
`core.ref` 取依赖），测试可直接注入 fake transport。
"""

from __future__ import annotations

from typing import Protocol

from core.ref import get_xiaoai


class ShellTransport(Protocol):
    async def run_shell(self, script: str, timeout_ms: float = 6000) -> str: ...


class XiaoAIShellTransport:
    """把调用转发给当前 XiaoAI 客户端（Rust RPC `run_shell`）。"""

    async def run_shell(self, script: str, timeout_ms: float = 6000) -> str:
        xiaoai = get_xiaoai()
        if xiaoai is None:
            raise RuntimeError("设备传输未初始化：未注册 XiaoAI 客户端")
        return await xiaoai.run_shell(script, timeout=timeout_ms)
