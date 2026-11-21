"""CCH now-playing 编排契约测试（票 03/04/13 服务端，2026-11-20）。

覆盖：
- 票 03 换代编排：STATE finish/stop → 逐条撤旧票 + cast 下一首；冷却窗内重复
  finish 不二次 advance；队列耗尽 → queue_exhausted（回退原厂链，不动作）。
- 票 04 TTS 恢复：resume_after_tts 幂等（已 play 不动）；resume 打死实例 → re-cast；
  speak_with_resume：pre 不活跃只播报 / 在管则登记定时器并到点恢复 / 新 speak 顶旧定时器。
- 票 13 收藏幂等：读 isFavorite 决定方向（一致 noop / 不一致 tap / 态未知拒发）。
- 双关记录：record_event 只记录不编排（META/isFavorite 仍可回读）。

纯离线：cch_cast 的下行函数全部模块属性替换为 fake，不联网、不连设备。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.music import cch_cast  # noqa: E402
from core.music import cch_np  # noqa: E402
from core.tools import song_request as sr  # noqa: E402


class CchNpTestBase(unittest.IsolatedAsyncioTestCase):
    """公共夹具：清内存态、重置冷却窗、备份 cch_cast/song_request 被替换属性。"""

    def setUp(self):
        cch_np._np_state.update({"meta": {}, "state": {}})
        cch_np._last_advance_at = -1e9
        sr._cch_state.update({"entry": 0, "items": []})
        self._saved = {}
        for name in ("resolve_ticket_url", "kwqq_meta", "build_render_data",
                     "cast_native", "cast_native_simple", "revoke", "speak"):
            self._saved[name] = getattr(cch_cast, name)

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(cch_cast, name, fn)
        if cch_np._speak_timer is not None:
            cch_np._speak_timer.cancel()
            cch_np._speak_timer = None
        sr._cch_state.update({"entry": 0, "items": []})

    # ---- fake 构造 ----

    def fake_downlink(self, *, cast_ok=True, simple_ok=True, revoke_log=None):
        """替换 cch_cast 下行函数为内存 fake。返回记录 dict。"""
        rec = {"cast": [], "simple": [], "revoke": [], "speak": []}

        async def fake_resolve(ref, *, session, epoch, entry):
            return f"http://songloft/media?media_ticket={entry}"

        async def fake_meta(song_id, source="kuwo"):
            return {"name": f"歌{song_id}", "singer": "歌手", "album": "专辑",
                    "cover": "http://cover", "duration": 180, "id": song_id}

        def fake_build(ticket_url, item, meta):
            return {"audioUrl": ticket_url, "audioName": meta.get("name", "")}

        async def fake_cast(render_data):
            rec["cast"].append(render_data)
            return cast_ok, "http 200"

        async def fake_simple(command):
            rec["simple"].append(command)
            return simple_ok, ("http 200" if simple_ok else "http 500")

        async def fake_revoke(session, epoch, entry):
            rec["revoke"].append(entry)
            return True

        async def fake_speak(text):
            rec["speak"].append(len(text or ""))
            return True, "http 200"

        cch_cast.resolve_ticket_url = fake_resolve
        cch_cast.kwqq_meta = fake_meta
        cch_cast.build_render_data = fake_build
        cch_cast.cast_native = fake_cast
        cch_cast.cast_native_simple = fake_simple
        cch_cast.revoke = fake_revoke
        cch_cast.speak = fake_speak
        if revoke_log is not None:
            revoke_log.extend(rec["revoke"])
        return rec


class AdvanceTest(CchNpTestBase):
    """票 03：STATE finish → 撤旧票 + cast 下一首。"""

    async def test_finish_advances_and_revokes_old_entry(self):
        rec = self.fake_downlink()
        sr._cch_state.update({"entry": 0, "items": [
            {"source": "kwqq", "id": "s1", "name": "一"},
            {"source": "kwqq", "id": "s2", "name": "二"},
        ]})
        action = await cch_np.on_np_event("STATE", {"statusStr": "finish", "audioStatus": "1"})
        self.assertEqual(action["action"], "advanced")
        self.assertEqual(action["index"], 1)
        self.assertEqual(sr._cch_state["entry"], 1)
        self.assertEqual(rec["revoke"], ["s1"], "换代前必须逐条撤刚播完那条的旧票")
        self.assertEqual(len(rec["cast"]), 1)
        self.assertEqual(rec["cast"][0]["audioName"], "歌s2")

    async def test_duplicate_finish_within_cooldown_suppressed(self):
        rec = self.fake_downlink()
        sr._cch_state.update({"entry": 0, "items": [
            {"source": "kwqq", "id": "s1"}, {"source": "kwqq", "id": "s2"},
            {"source": "kwqq", "id": "s3"},
        ]})
        first = await cch_np.on_np_event("STATE", {"statusStr": "stop", "audioStatus": "8"})
        self.assertEqual(first["action"], "advanced")
        second = await cch_np.on_np_event("STATE", {"statusStr": "finish", "audioStatus": "1"})
        self.assertEqual(second["action"], "advance_cooldown")
        self.assertEqual(len(rec["cast"]), 1, "冷却窗内重复 finish 不得二次 advance（防跳曲）")
        self.assertEqual(sr._cch_state["entry"], 1)

    async def test_queue_exhausted_falls_back_to_native(self):
        rec = self.fake_downlink()
        sr._cch_state.update({"entry": 1, "items": [{"source": "kwqq", "id": "s1"},
                                                    {"source": "kwqq", "id": "s2"}]})
        action = await cch_np.on_np_event("STATE", {"statusStr": "finish", "audioStatus": "1"})
        self.assertEqual(action["action"], "queue_exhausted")
        self.assertEqual(rec["revoke"], ["s2"], "耗尽时撤当前条旧票")
        self.assertEqual(rec["cast"], [], "回退原厂链：不 cast、不动作")

    async def test_cast_http_fail_still_reports_advanced_with_ok_false(self):
        """cast_native 的 ok 只是 HTTP 面（生效与否看设备侧 observable）：
        ok=False 不算 advance_failed，entry 照常推进，撤旧票照常。"""
        rec = self.fake_downlink(cast_ok=False)
        sr._cch_state.update({"entry": 0, "items": [{"source": "kwqq", "id": "s1"},
                                                    {"source": "kwqq", "id": "s2"}]})
        action = await cch_np.on_np_event("STATE", {"statusStr": "finish", "audioStatus": "1"})
        self.assertEqual(action["action"], "advanced")
        self.assertFalse(action["ok"])
        self.assertEqual(rec["revoke"], ["s1"], "cast 失败不影响撤旧票")
        self.assertEqual(sr._cch_state["entry"], 1)

    async def test_prepare_exception_reports_advance_failed(self):
        """prepare/网络异常 → advance_failed（entry 已推进，等下一事件）。"""
        self.fake_downlink()

        async def boom(ref, *, session, epoch, entry):
            raise RuntimeError("prepare timeout")

        cch_cast.resolve_ticket_url = boom
        sr._cch_state.update({"entry": 0, "items": [{"source": "kwqq", "id": "s1"},
                                                    {"source": "kwqq", "id": "s2"}]})
        action = await cch_np.on_np_event("STATE", {"statusStr": "finish", "audioStatus": "1"})
        self.assertEqual(action["action"], "advance_failed")
        self.assertEqual(sr._cch_state["entry"], 1)

    async def test_suspend_and_play_only_recorded(self):
        self.fake_downlink()
        sr._cch_state.update({"entry": 0, "items": [{"id": "s1"}, {"id": "s2"}]})
        self.assertEqual(
            (await cch_np.on_np_event("STATE", {"statusStr": "suspend", "audioStatus": "15"}))["action"],
            "suspend_recorded")
        self.assertEqual(
            (await cch_np.on_np_event("STATE", {"statusStr": "play", "audioStatus": "0"}))["action"],
            "play_recorded")
        self.assertEqual(sr._cch_state["entry"], 0, "suspend/play 不推进换代")


class RecordEventTest(CchNpTestBase):
    """双关（编排关闭）只记录不动作，META/isFavorite 仍可回读（票 13 读态源）。"""

    async def test_record_event_stores_without_orchestration(self):
        rec = self.fake_downlink()
        cch_np.record_event("META", {"Title": "歌", "isFavorite": "1"})
        cch_np.record_event("STATE", {"statusStr": "finish", "audioStatus": "1"})
        snap = cch_np.snapshot()
        self.assertEqual(snap["meta"]["extras"]["isFavorite"], "1")
        self.assertEqual(snap["state"]["extras"]["statusStr"], "finish")
        self.assertEqual(rec["cast"], [], "双关不编排")
        self.assertEqual(sr._cch_state["entry"], 0)


class ResumeTest(CchNpTestBase):
    """票 04：resume 幂等 / 死实例 re-cast。"""

    async def test_already_playing_skips_resume(self):
        rec = self.fake_downlink()
        cch_np._np_state["state"] = {"extras": {"statusStr": "play", "audioStatus": "0"}}
        result = await cch_np.resume_after_tts()
        self.assertEqual(result["action"], "already_playing")
        self.assertEqual(rec["simple"], [], "STATE 已回 play 不重复 resume")

    async def test_resume_ok(self):
        rec = self.fake_downlink()
        result = await cch_np.resume_after_tts()
        self.assertEqual(result["action"], "resumed")
        self.assertEqual(rec["simple"], [2])

    async def test_dead_instance_recasts_current(self):
        rec = self.fake_downlink(simple_ok=False)
        sr._cch_state.update({"entry": 0, "items": [{"source": "kwqq", "id": "s1"}]})
        result = await cch_np.resume_after_tts()
        self.assertEqual(result["action"], "recast")
        self.assertEqual(rec["simple"], [2])
        self.assertEqual(len(rec["cast"]), 1, "resume 打在死实例（STATE 8）→ 重新 cast 当前曲目")

    async def test_recast_skipped_when_no_queue(self):
        rec = self.fake_downlink(simple_ok=False)
        result = await cch_np.resume_after_tts()
        self.assertEqual(result["action"], "recast_skipped")


class SpeakResumeTest(CchNpTestBase):
    """票 04：speak_with_resume 编排（pre 判据 / 定时器恢复 / 顶旧定时器）。"""

    async def test_speak_only_when_not_managed(self):
        rec = self.fake_downlink()
        sr._cch_state.update({"entry": 0, "items": []})
        result = await cch_np.speak_with_resume("你好", delay_s=0.05)
        self.assertEqual(result["action"], "speak_only")
        await asyncio.sleep(0.15)
        self.assertEqual(rec["simple"], [], "不在管（队列空）不编排恢复")

    async def test_speak_only_when_gate_closed(self):
        rec = self.fake_downlink()
        sr._cch_state.update({"entry": 0, "items": [{"id": "s1"}]})
        orig = cch_np.np_enabled
        cch_np.np_enabled = lambda: False
        try:
            result = await cch_np.speak_with_resume("你好", delay_s=0.05)
        finally:
            cch_np.np_enabled = orig
        self.assertEqual(result["action"], "speak_only")
        await asyncio.sleep(0.15)
        self.assertEqual(rec["simple"], [], "闸门关不编排恢复")

    async def test_speak_schedules_resume(self):
        rec = self.fake_downlink()
        sr._cch_state.update({"entry": 0, "items": [{"id": "s1"}]})
        orig = cch_np.np_enabled
        cch_np.np_enabled = lambda: True
        try:
            result = await cch_np.speak_with_resume("播报文本", delay_s=0.05)
        finally:
            cch_np.np_enabled = orig
        self.assertEqual(result["action"], "speak_scheduled_resume")
        await asyncio.sleep(0.3)
        self.assertEqual(rec["simple"], [2], "定时器到点且 STATE 非 play → resume")

    async def test_new_speak_supersedes_old_timer(self):
        rec = self.fake_downlink()
        sr._cch_state.update({"entry": 0, "items": [{"id": "s1"}]})
        orig = cch_np.np_enabled
        cch_np.np_enabled = lambda: True
        try:
            await cch_np.speak_with_resume("第一段", delay_s=5.0)
            await cch_np.speak_with_resume("第二段", delay_s=0.05)
        finally:
            cch_np.np_enabled = orig
        await asyncio.sleep(0.3)
        self.assertEqual(rec["simple"], [2], "新 speak 顶掉旧定时器：只恢复一次，不等第一段的 5s")
        self.assertIsNotNone(cch_np._speak_timer)

    async def test_speak_failed_no_resume(self):
        rec = self.fake_downlink()

        async def fail_speak(text):
            return False, "http 500"

        cch_cast.speak = fail_speak
        sr._cch_state.update({"entry": 0, "items": [{"id": "s1"}]})
        result = await cch_np.speak_with_resume("文本", delay_s=0.05)
        self.assertEqual(result["action"], "speak_failed")
        await asyncio.sleep(0.15)
        self.assertEqual(rec["simple"], [], "speak 失败不登记恢复")

    async def test_timer_skips_when_state_recovered(self):
        rec = self.fake_downlink()
        sr._cch_state.update({"entry": 0, "items": [{"id": "s1"}]})
        cch_np._np_state["state"] = {"extras": {"statusStr": "play", "audioStatus": "0"}}
        orig = cch_np.np_enabled
        cch_np.np_enabled = lambda: True
        try:
            result = await cch_np.speak_with_resume("文本", delay_s=0.05)
        finally:
            cch_np.np_enabled = orig
        self.assertEqual(result["action"], "speak_scheduled_resume")
        await asyncio.sleep(0.2)
        self.assertEqual(rec["simple"], [], "原厂已自行 resume（STATE play）→ 定时器跳过")


class CollectFavoriteTest(CchNpTestBase):
    """票 13：收藏幂等（读 isFavorite 决定 collect 方向，不盲发翻转）。"""

    async def test_noop_when_already_favorite(self):
        rec = self.fake_downlink()
        cch_np._np_state["meta"] = {"extras": {"Title": "歌", "isFavorite": "1"}}
        result = await cch_np.collect_favorite(True)
        self.assertEqual(result["action"], "noop")
        self.assertEqual(result["favorite"], True)
        self.assertEqual(rec["simple"], [], "已收藏不再发 like tap")

    async def test_tap_unlike_when_intent_false(self):
        rec = self.fake_downlink()
        cch_np._np_state["meta"] = {"extras": {"isFavorite": "1"}}
        result = await cch_np.collect_favorite(False)
        self.assertEqual(result["action"], "tapped")
        self.assertEqual(result["favorite"], False)
        self.assertEqual(rec["simple"], [9], "已收藏 + 意图取消 → COMMAND 9 tap")

    async def test_tap_like_when_not_favorite(self):
        rec = self.fake_downlink()
        cch_np._np_state["meta"] = {"extras": {"isFavorite": "0"}}
        result = await cch_np.collect_favorite(True)
        self.assertEqual(result["action"], "tapped")
        self.assertEqual(rec["simple"], [9])

    async def test_unknown_state_refuses(self):
        rec = self.fake_downlink()
        result = await cch_np.collect_favorite(True)
        self.assertEqual(result["action"], "unknown_state")
        self.assertEqual(rec["simple"], [], "态未知不盲发翻转（失败关闭）")

    async def test_garbage_value_treated_unknown(self):
        rec = self.fake_downlink()
        cch_np._np_state["meta"] = {"extras": {"isFavorite": "maybe"}}
        result = await cch_np.collect_favorite(True)
        self.assertEqual(result["action"], "unknown_state")
        self.assertEqual(rec["simple"], [])

    async def test_tap_failed_reported(self):
        rec = self.fake_downlink(simple_ok=False)
        cch_np._np_state["meta"] = {"extras": {"isFavorite": "0"}}
        result = await cch_np.collect_favorite(True)
        self.assertEqual(result["action"], "tap_failed")
        self.assertEqual(rec["simple"], [9])


if __name__ == "__main__":
    unittest.main()
