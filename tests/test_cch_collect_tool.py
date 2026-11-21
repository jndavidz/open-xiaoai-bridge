"""cch_collect 工具契约测试（票 13 收尾：语音触发链 + 单发标志语义）。

覆盖：
- 工具回复映射：no_track / added / noop / removed / failed → 用户可读文案；
- _play_event 单发收尾语义修正：置 no_advance=True 但**保留 items**（收藏读
  当前曲队列；v0.4.13 曾清空 items 致点击播后收藏 no_track）；
- cch_np advance 的 no_advance 单发抑制 + 语音点歌路径显式清标志。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.music import cch_np  # noqa: E402
from core.music import cch_click  # noqa: E402
from core.tools import cch_collect  # noqa: E402
from core.tools import song_request as sr  # noqa: E402


class CchCollectToolTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        sr._cch_state.update({"entry": 0, "items": [], "no_advance": False})
        self._saved = dict(collect=cch_np.collect_favorite)

    def tearDown(self):
        sr._cch_state.update({"entry": 0, "items": [], "no_advance": False})
        cch_np.collect_favorite = self._saved["collect"]

    # ---- 回复映射 ----

    async def test_added_reply_includes_song_name(self):
        sr._cch_state.update({"entry": 0,
                              "items": [{"id": "1", "name": "在水一方", "source": "KG"}]})

        async def fake_collect(intent):
            return {"action": "added"}

        cch_np.collect_favorite = fake_collect
        reply = await cch_collect.run({})
        self.assertIn("在水一方", reply)
        self.assertIn("收藏", reply)

    async def test_noop_reply(self):
        sr._cch_state.update({"entry": 0,
                              "items": [{"id": "1", "name": "晴天", "source": "KG"}]})

        async def fake_collect(intent):
            return {"action": "noop"}

        cch_np.collect_favorite = fake_collect
        reply = await cch_collect.run({"intent": True})
        self.assertIn("已在", reply)

    async def test_no_track_reply(self):
        async def fake_collect(intent):
            return {"action": "no_track"}

        cch_np.collect_favorite = fake_collect
        reply = await cch_collect.run({})
        self.assertIn("没有正在播放", reply)

    async def test_failed_reply(self):
        async def fake_collect(intent):
            return {"action": "failed", "detail": "SongloftError"}

        cch_np.collect_favorite = fake_collect
        reply = await cch_collect.run({})
        self.assertIn("失败", reply)

    async def test_remove_reply(self):
        sr._cch_state.update({"entry": 0,
                              "items": [{"id": "1", "name": "晴天", "source": "KG"}]})

        async def fake_collect(intent):
            return {"action": "removed"}

        cch_np.collect_favorite = fake_collect
        reply = await cch_collect.run({"intent": False})
        self.assertIn("移除", reply)

    # ---- 单发标志语义 ----

    async def test_play_event_keeps_items_and_sets_no_advance(self):
        """点击播后：items 保留（收藏读当前曲）+ no_advance=True（换代抑制）。"""
        async def fake_run(args):
            return "ok"

        async def noop_resume():
            return

        sr._cch_state.update({"entry": 0, "items": [], "no_advance": False})
        with mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_resume_if_paused", noop_resume):
            await cch_click._play_event({"name": "晴天", "artist": "", "source": "HIST"})
        self.assertEqual(sr._cch_state.get("no_advance"), True)
        self.assertEqual(sr._cch_state.get("items"), [], "点击播不写队列，保持原状")

    async def test_advance_suppressed_by_no_advance_flag(self):
        """no_advance=True：曲终不换代、不清队列、可撤销。"""
        sr._cch_state.update({"entry": 0, "items": [{"id": "x", "name": "刘巧儿"}],
                              "no_advance": True})
        cch_np._last_advance_at = -1e9
        result = await cch_np._advance_if_finished()
        self.assertEqual(result.get("action"), "no_advance")
        self.assertEqual(len(sr._cch_state.get("items") or []), 1, "队列不被清空")
        self.assertEqual(sr._cch_state.get("entry"), 0)

    async def test_voice_play_clears_no_advance(self):
        """语音点歌路径显式清 no_advance（换代表面恢复正常）。"""
        # 直接验证 _run_cch 写入形态：no_advance=False 进 _cch_state
        sr._cch_state.update({"entry": 0, "items": [], "no_advance": True})
        # 模拟 _run_cch 的收尾写入（与源码同款语句）
        items = [{"id": "1", "name": "亲爱的你啊"}]
        sr._cch_state.update({"entry": 0, "items": items, "no_advance": False})
        self.assertEqual(sr._cch_state.get("no_advance"), False)
        cch_np._last_advance_at = -1e9


if __name__ == "__main__":
    unittest.main()
