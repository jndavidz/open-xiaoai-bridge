"""家庭工具层（Agent 1.5 / 阶段 1.5）。

只读工具注册表：模型通过 function calling 调用，执行本地/局域网内的
安全查询，不触达任何写操作（写操作分级见 runbook T8.4，本期未实现）。

设计要点（lineage §5 阶段 1.5）：
- 工具模块自带 OpenAI function 规格（TOOL_SPEC）与 async run(args)->str；
- 新增一个工具 = 加一个模块文件，registry 零改动自动发现；
- 每个 run() 必须 try/except 降级：失败返回错误串而非抛异常，
  否则一次工具错误会令整个对话轮崩溃（模型拿到错误串可自行纠正）。
"""

from .registry import get_all_tools, execute_tool

__all__ = ["get_all_tools", "execute_tool"]
