# Spec: bridge B-32 —— TurnTrace 落盘（A4）

Status: completed（落盘代码已落地 @ bridge 67883a1。✅ 2026-10-28 复核：`deploy/config.py` 的 `turn_trace.enabled/persist` 均已为 True（dev 基线仍 False，分侧把守）⇒ 数据源已就绪，`data/turntrace/` 待自然积累 ≥40 轮后按 SLO 票口径复核，见 `ws: docs/plan/pending-followups.md` §4（自动到期观察项））

来源：长时间工作计划 A 区第 4 项（pending-followups 3a 的前置改进）。

## Problem

每轮 [TurnTrace] 延迟数据只在容器日志里——容器 recreate 即丢（首轮 ~23 轮 SLO
数据已因此丢失）。≥40 轮统计复核没有可持续的数据源。

## Solution

`turn_trace.persist` 开关（env `TURN_TRACE_PERSIST` 覆盖）：开启时每轮 summary
全量 JSONL 落 `data/turntrace/turntrace-<date>.jsonl`（数值 + UTC 时间戳，
无 PII——口径同日志行）；写入经 executor（usage_ledger 同款）；单开 persist
也建轮落盘（`_recording()` 计入）。

## Acceptance

- `tests/test_turn_trace_persist.py`：env 解析、落盘行含 ts 与各段 ms、
  abandon 路径经 _emit 落盘、并发写锁串行全量。
- config：根 persist=False；deploy persist=True（生产即时受益）。
