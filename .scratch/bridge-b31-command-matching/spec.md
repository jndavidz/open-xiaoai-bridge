# Spec: bridge B-31 —— 指令匹配归一化 + 别名键底座（A2 / 评审 W4 机制层）

Status: completed（机制已落地 @ bridge 27ae7fe；**数据面已上生产**——`deploy/config.py` 已建 `COMMAND_ALIASES` 并接线 `aliases=`，表空 = 逐字等价，待用户裁决填值，见 `ws: docs/plan/pending-followups.md` §1.4）

## Problem

指令表精确字符串匹配对原生 ASR 转写抖动零防御：「HiFi模式→哈哈模式」、
「打开Kodi→打开CODY」、「打开Kodi→打开电脑」曾误触发 WOL 两次
（deploy/config.py §XIAOAI_COMMANDS 实测记录）。词表与指令键两组词源
各自维护、注释承认会漂移。

## Solution（机制层，**零行为变化**——数据面等用户裁决）

1. `core/commands/matching.py` 纯函数：`normalize_command_text`
   （NFKC 全角→半角 + 去空白标点 + 拉丁小写）与 `resolve_command_key`
   （归一化精确 → 别名映射）。**不含模糊/拼音匹配**——「宁可少截不可错吞」，
   拼音级容错的放行范围属用户裁决（`ws: docs/plan/pending-followups.md` §1.4），只留机制。
2. `route_before_wakeup` 新增可选 `aliases` 参数：两张指令表查找改经
   `resolve_command_key`；`停止聆听` 同口径归一化（标点抖动免疫）。
   别名表缺省/空 = 现状逐字等价（守门测试钉住）。
3. deploy/config.py 新增 `COMMAND_ALIASES: dict = {}`（含转写抖动候选样例
   注释）并透传钩子；根 config 不填数据。

## Acceptance

- `tests/test_command_matching.py`：归一化（大小写/标点/全角）、别名映射、
  悬空别名忽略、**miss 仍是 miss**（归一化≠模糊）、无别名 = 逐字等价。
- 全量基线不回归（含 deploy 数据面守门 test_commands / test_kws_audit）。
