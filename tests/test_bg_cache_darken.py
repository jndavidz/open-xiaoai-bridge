#!/usr/bin/env python3
"""守门测试：全屏歌词背景暗化 seam（2026-11-23 浅色封面白字看不清 bug）。

被测 seam: _overlay_alpha(mean_luma) -> float  （纯函数，无 PIL 依赖）
  - 输入：虚化图的平均亮度 L（0..255）
  - 输出：叠黑罩 alpha（0..1）
  - 规约：浅色封面多叠（白字可读），深色封面少叠（保留氛围），单调不减

真机背景：原厂全屏歌词页直接渲染 bridge 下发的 backgroundImage（红图验证），
白色歌词在亮背景上看不清 → 虚化图需叠暗罩。深罩保证 L_out = L*(1-alpha) <= ~105。

用法: python3 tests/test_bg_cache_darken.py [path-to-bg_cache.py]
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[1] / "core" / "music" / "bg_cache.py"


def load_overlay_alpha(bg_cache_path):
    """从 bg_cache.py 提取 _overlay_alpha 纯函数（不触发 PIL import）。"""
    src = open(bg_cache_path, encoding="utf-8").read()
    start = src.index("def _overlay_alpha")
    # 到下一个顶层 def / 常量
    end = src.find("\ndef ", start + 1)
    if end < 0:
        end = len(src)
    block = src[start:end]
    mod = types.ModuleType("bg_darken_under_test")
    # 提供函数用到的常量（若从模块级引用）
    for line in src.splitlines():
        if line.startswith("_DARK_TARGET") or line.startswith("_DARK_MIN") or line.startswith("_DARK_MAX"):
            exec(line, mod.__dict__)
    exec(compile(block, "<bg_cache.py>", "exec"), mod.__dict__)
    return mod._overlay_alpha


def test_white_cover_darkened_enough():
    a = overlay_alpha(255)
    out = 255 * (1 - a)
    assert out <= 110, "white cover must be darkened to L<=110 for white lyrics, got %.1f" % out


def test_dark_cover_keeps_mood():
    a = overlay_alpha(40)
    assert a <= 0.25, "dark cover should stay dark-ish (alpha<=0.25), got %.3f" % a


def test_monotonic():
    vals = [overlay_alpha(x) for x in (20, 60, 100, 140, 180, 220, 255)]
    assert all(vals[i] <= vals[i+1] for i in range(len(vals)-1)), \
        "alpha must be non-decreasing with brightness"


def test_bounded():
    for x in (0, 1, 128, 254, 255, 300, -5):
        a = overlay_alpha(x)
        assert 0.0 <= a <= 1.0, "alpha must be in [0,1], got %.3f at L=%s" % (a, x)


def test_mid_gray_moderate():
    a = overlay_alpha(128)
    assert 0.2 <= a <= 0.6, "mid-gray alpha should be moderate, got %.3f" % a


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT)
    overlay_alpha = load_overlay_alpha(p)
    tests = [test_white_cover_darkened_enough, test_dark_cover_keeps_mood,
             test_monotonic, test_bounded, test_mid_gray_moderate]
    failures = 0
    for fn in tests:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as e:
            failures += 1
            print("FAIL", fn.__name__, "-", type(e).__name__, str(e))
    print("RESULT", "FAIL" if failures else "OK", "failures=", failures)
    sys.exit(1 if failures else 0)
