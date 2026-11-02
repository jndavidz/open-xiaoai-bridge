"""逐词门槛词表编译（music-far spec 票 03）—— 纯函数、仅标准库。

sherpa-onnx keywords.txt 官方语法（k2-fsa 文档已核实，2026-11-03）：
    <tokens> [:<boost>] [#<threshold>] [@<显示名>]
    - ":x" = 该词的 boosting score（影响 beam search 存活，热词加成同源）；
    - "#x" = 该词的触发门槛（trigger threshold）；两项均可省略；
    - 无后缀词沿用全局 keywords_score / keywords_threshold。
放 core/utils 是为了让 keywords.py 与 shadow.py（各自独立加载）共用同一
编译口径，而不触发 core.services.audio.kws 包的 heavyweight 初始化。

词表配置（数据面在 deploy/config.py）：kws.keyword_gates = {词: {...}}；
读取校验见 keywords.get_word_gates（依赖 ConfigManager，不入本模块）。
"""

from __future__ import annotations

from pathlib import Path


def compile_keyword_line(tokens_row, word: str, gate: dict | None = None) -> str:
    """一行词表：tokens + 可选 :boost / #threshold + @显示名（原词，恒在末位）。"""
    line = " ".join(tokens_row)
    if gate:
        boost = gate.get("boost")
        threshold = gate.get("threshold")
        if boost is not None:
            line += f" :{float(boost):g}"
        if threshold is not None:
            line += f" #{float(threshold):g}"
    return f"{line} @{word}"


def parse_keyword_line(line: str) -> dict:
    """解析一行词表 → {tokens, display, boost, threshold, malformed}。

    display 丢失为 None；无法解析的后缀进 malformed（自检用）。
    """
    tokens: list[str] = []
    display = None
    boost = None
    threshold = None
    malformed: list[str] = []
    for item in line.split():
        if item.startswith("@"):
            display = item[1:] or None
        elif boost is None and item.startswith(":") and len(item) > 1:
            try:
                boost = float(item[1:])
            except ValueError:
                malformed.append(item)
        elif threshold is None and item.startswith("#") and len(item) > 1:
            try:
                threshold = float(item[1:])
            except ValueError:
                malformed.append(item)
        else:
            tokens.append(item)
    return {
        "tokens": tokens,
        "display": display,
        "boost": boost,
        "threshold": threshold,
        "malformed": malformed,
    }


def verify_keyword_lines(path, words: list[str]) -> list[str]:
    """词表自检：行数对齐 + 每行显示名 = 原词 + 后缀可解析。

    返回问题列表（空 = 通过）；调用方（keywords.main 启动期）负责告警。
    """
    problems: list[str] = []
    try:
        lines = [
            line
            for line in Path(path).read_text(encoding="utf8").splitlines()
            if line.strip()
        ]
    except OSError as exc:
        return [f"词表不可读：{exc}"]
    if len(lines) != len(words):
        problems.append(f"词表行数 {len(lines)} ≠ 词数 {len(words)}")
    for index, (line, word) in enumerate(zip(lines, words), 1):
        parsed = parse_keyword_line(line)
        if parsed["display"] != word:
            problems.append(f"第 {index} 行显示名 {parsed['display']!r} ≠ 原词 {word!r}")
        if parsed["malformed"]:
            problems.append(f"第 {index} 行后缀非法：{parsed['malformed']}")
    return problems
