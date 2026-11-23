#!/usr/bin/env python3
"""守门测试：播控页显示用 audioName 拼接「歌名 - 歌手 - 《专辑》」。

被测 seam: _display_audio_name(name, singer, album) -> str （纯函数）
规约：
  - 三项齐全 → "歌名 - 歌手 - 《专辑》"
  - 无专辑 → "歌名 - 歌手"
  - 无歌手无专辑 → "歌名"（原样，不产生空壳）
  - 空白项视为缺失
真机背景：原厂播控页 audioName 直接显示；歌手/专辑分列 audioExt.audioAnchor/albumName
不显眼，用户要求拼进主标题。meta 常为 None（kwqq_meta 实测全 None），需靠 item 回退值。
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


def test_full():
    assert fn("晴天", "周杰伦", "叶惠美") == "晴天 - 周杰伦 - 《叶惠美》"


def test_no_album():
    assert fn("晴天", "周杰伦", "") == "晴天 - 周杰伦"


def test_no_singer_no_album():
    assert fn("晴天", "", "") == "晴天"


def test_whitespace_treated_missing():
    assert fn("  晴天  ", "  ", "  ") == "晴天"


def test_singer_no_album_variants():
    assert fn("稻香", "周杰伦", None) == "稻香 - 周杰伦"


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT)
    fn = load_fn(p)
    tests = [test_full, test_no_album, test_no_singer_no_album,
             test_whitespace_treated_missing, test_singer_no_album_variants]
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
