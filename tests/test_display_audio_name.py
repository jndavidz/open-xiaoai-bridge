#!/usr/bin/env python3
"""守门测试：播控页主标题 audioName =「歌手 - 歌名」（2026-11-24 用户更正，
与全屏歌词页左下角格式一致）。

被测 seam: _display_audio_name(name, singer, album) -> str （纯函数）
规约（最终）：
  - 歌手 + 歌名都在 → "歌手 - 歌名"（专辑参数忽略，用户要求不显示）
  - 只有其一 → 该项本身
  - 空白项视为缺失
真机背景：原厂标题控件不渲染 \n（注入 \n 被吞成空格，两行方案不可行），
故单行。meta 常为 None，靠 item 回退。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[1] / "core" / "music" / "cch_cast.py"


def load_fn(path):
    src = open(path, encoding="utf-8").read()
    start = src.index("def _display_audio_name")
    end = src.find("\ndef ", start + 1)
    block = src[start:end if end > 0 else len(src)]
    mod = types.ModuleType("display_name_under_test")
    exec(compile(block, "<cch_cast.py>", "exec"), mod.__dict__)
    return mod._display_audio_name


def test_full_ignores_album():
    assert fn("晴天", "周杰伦", "叶惠美") == "周杰伦 - 晴天"


def test_no_singer():
    assert fn("晴天", "", "") == "晴天"


def test_whitespace():
    assert fn("  晴天  ", "  ", "  叶惠美 ") == "晴天"


def test_none_values():
    assert fn("晴天", None, None) == "晴天"


def test_both_present_no_album():
    assert fn("稻香", "周杰伦", None) == "周杰伦 - 稻香"


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT)
    fn = load_fn(p)
    tests = [test_full_ignores_album, test_no_singer, test_whitespace,
             test_none_values, test_both_present_no_album]
    failures = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except Exception as e:
            failures += 1
            print("FAIL", t.__name__, "-", type(e).__name__, str(e))
    print("RESULT", "FAIL" if failures else "OK", "failures=", failures)
    sys.exit(1 if failures else 0)
