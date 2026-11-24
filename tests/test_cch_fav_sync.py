"""守门测试：CCH 收藏即时同步 worker 的纯函数 seam（2026-11-24）。

背景：Songloft 合入上游后提供 `GET /playlists/favorites/changes?cursor=` 长轮询端点
（25s，成员提交后唤醒，changed=true）；bridge 收到变化后全量 push_favorites 写设备
favorites.json，供 gadget 在 V2 HIT 时替换原厂收藏 tab。

被测 seam（纯函数，无网络/DB 依赖）:
  - apply_change_response(prev_cursor, response) -> (new_cursor, changed)
  - should_push(prev_cursor, new_cursor, changed) -> bool

规约（对齐 Songloft services/favorite_changes.go 的 cursor 语义）:
  - 首次（prev=None）→ changed=True（重连/重启立即同步）
  - cursor 相同且 changed=False → 不 push
  - changed=True → push（无论 cursor 变否）
  - cursor 变了但 changed=False → 仍 push（防漏：服务端可能只换 cursor）
  - 响应须同时含非空字符串 cursor 与 bool changed，否则视为 malformed → 回到
    None（下次触发首同步，宁可多写）；should_push 因其 prev=None 必为真
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[1] / "core" / "music" / "cch_fav_sync.py"


def load_mod(path):
    src = open(path, encoding="utf-8").read()
    mod = types.ModuleType("fav_sync_under_test")
    mod.__dict__["__name__"] = "fav_sync_under_test"
    start = src.index("def apply_change_response")
    cut = src.find("\nasync def ", start)
    block = src[start:cut if cut > 0 else len(src)]
    exec(compile(block, "<cch_fav_sync.py>", "exec"), mod.__dict__)
    return mod


class CchFavSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_mod(str(DEFAULT))

    def test_first_sync_always_pushes(self):
        new, changed = self.mod.apply_change_response(None, {"cursor": "ep:7", "changed": True})
        self.assertEqual(new, "ep:7")
        self.assertTrue(changed)
        self.assertTrue(self.mod.should_push(None, new, changed))

    def test_unchanged_cursor_no_push(self):
        new, changed = self.mod.apply_change_response("ep:7", {"cursor": "ep:7", "changed": False})
        self.assertEqual(new, "ep:7")
        self.assertFalse(changed)
        self.assertFalse(self.mod.should_push("ep:7", new, changed))

    def test_changed_true_pushes_even_if_cursor_same(self):
        new, changed = self.mod.apply_change_response("ep:7", {"cursor": "ep:7", "changed": True})
        self.assertTrue(self.mod.should_push("ep:7", new, changed))

    def test_cursor_advance_without_flag_still_pushes(self):
        # 防御性：cursor 变了即视为可能漏报，宁可多写一次
        self.assertTrue(self.mod.should_push("ep:7", "ep:8", False))

    def test_malformed_response_fails_to_resync(self):
        bad_responses = [
            {}, {"cursor": "ep:7"}, {"changed": True}, None, "oops", [],
            {"cursor": 123, "changed": True},     # cursor 非 str
            {"cursor": "", "changed": True},      # cursor 空串
            {"cursor": "ep:7", "changed": 1},     # changed 非 bool
        ]
        for bad in bad_responses:
            with self.subTest(bad=bad):
                new, changed = self.mod.apply_change_response("ep:7", bad)
                self.assertIsNone(new, f"{bad!r} must reset cursor")
                self.assertFalse(changed, f"{bad!r} must not report changed")
                # fail-closed 核心保证：malformed 后下次必 push（删掉失败重置也会红）
                self.assertTrue(self.mod.should_push("ep:7", new, changed))

    def test_should_push_first_when_no_prev(self):
        self.assertTrue(self.mod.should_push(None, "ep:9", False))


if __name__ == "__main__":
    unittest.main()
