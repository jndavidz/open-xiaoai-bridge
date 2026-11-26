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


class ReplayCurrentTest(CchNpTestBase):
    """票 06：曲终停止态点播放键 → 重新 cast 当前 entry（不推进队列、不换 entry）。"""

    async def test_replay_current_recasts_entry(self):
        from unittest import mock
        rec = self.fake_downlink()
        sr._cch_state.update({"entry": 1, "items": [
            {"source": "kwqq", "id": "s1", "name": "一"},
            {"source": "kwqq", "id": "s2", "name": "二"},
        ]})
        async def fake_wait(seconds):
            return True

        async def fake_attach(item, rd):
            return rd  # 歌词网络调用在单测里不触发

        with mock.patch.object(cch_cast, "m5_enabled", lambda: False), \
             mock.patch.object(cch_cast, "wait_np_play", fake_wait), \
             mock.patch.object(cch_cast, "attach_lyric", fake_attach):
            result = await cch_np.replay_current()
        self.assertEqual("replayed", result["action"])
        self.assertEqual(1, result["index"])
        self.assertTrue(result["ok"], "起播确认通过")
        self.assertEqual(1, len(rec["cast"]), "重新 cast 当前曲一次")
        self.assertEqual(1, sr._cch_state.get("entry"), "entry 不推进（与 advance 区别）")

    async def test_cast_entry_refreshes_lyric_and_display_on_m5(self):
        """换代/重播（m5 轨）必须刷新歌词+显示层，否则设备沿用上一首 lrc
        （2026-11-26 用户报「歌词对不上声音」）。"""
        from unittest import mock
        rec = self.fake_downlink()
        sr._cch_state.update({"entry": 0, "items": [
            {"source": "kwqq", "id": "s1", "name": "一"},
        ]})
        calls = []

        async def fake_attach(item, rd):
            calls.append("attach")
            return {**rd, "lyricUrl": "http://127.0.0.1:18081/s1.lrc"}

        async def fake_display(rd):
            calls.append("display")
            self.assertIn("lyricUrl", rd)
            return True

        async def fake_m5():
            return True

        async def fake_token():
            return True

        async def fake_play(cmd, **kwargs):
            return True, "OK"

        with mock.patch.object(cch_cast, "m5_enabled", fake_m5), \
             mock.patch.object(cch_cast, "host_token_deploy", fake_token), \
             mock.patch.object(cch_cast, "host_cmd", fake_play), \
             mock.patch.object(cch_cast, "attach_lyric", fake_attach), \
             mock.patch.object(cch_cast, "render_display", fake_display):
            result = await cch_np.replay_current()

        self.assertEqual(["attach", "display"], calls, "歌词注入先于显示补投")
        self.assertTrue(result["ok"])

    async def test_replay_current_no_items(self):
        rec = self.fake_downlink()
        result = await cch_np.replay_current()
        self.assertEqual("replay_no_item", result["action"])
        self.assertEqual([], rec["cast"])


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
    """票 13（2026-11-21 裁决）：收藏只写 Songloft，屏蔽原厂 tap。
    当前曲真相源 = 内存队列 _cch_state.items[entry]（§2.77-51，点播即时），设备侧
    current_song.json 仅兜底；测试里 mock current_song 模拟兜底路径。"""

    def setUp(self):
        super().setUp()
        from unittest import mock

        self._cur_song = {}
        async def fake_current():
            return dict(self._cur_song) if self._cur_song else None
        self._cur_patch = mock.patch.object(cch_np, "current_song", fake_current)
        self._cur_patch.start()
        self.addCleanup(self._cur_patch.stop)

    def _queue(self, item=None):
        self._cur_song = item or {
            "name": "青花瓷", "artist": "周杰伦", "album": "我很忙", "source": "kuwo",
            "id": "R1", "duration_s": 239}

    def _patches(self, added=1, fail=None):
        from unittest import mock

        from core.music import songloft_api

        calls = {"add": [], "remove": [], "ensure": [], "ensure_items": [], "push": 0}

        async def ensure(item, *, http):
            calls["ensure"].append(item.get("id"))
            calls["ensure_items"].append(dict(item))
            if fail == "ensure":
                raise songloft_api.SongloftError("down")
            return 42

        async def add(song_id, *, http):
            calls["add"].append(song_id)
            return {"added": added, "skipped": 1 - added}

        async def remove(song_id, *, http):
            calls["remove"].append(song_id)
            return {"message": "ok"}

        async def push(playlist_id=None):
            calls["push"] += 1
            return True, {"n": 0, "playlist_id": 1, "ok": True}

        return calls, (
            mock.patch.object(songloft_api, "ensure_song", ensure),
            mock.patch.object(songloft_api, "add_to_favorites", add),
            mock.patch.object(songloft_api, "remove_from_favorites", remove),
            # 票 25：默认拦下 favfile 同步，避免既有用例 fire 真实下行。
            mock.patch.object(cch_cast, "push_favorites", push),
        )

    async def test_no_track_when_queue_empty(self):
        self._cur_song = {}
        result = await cch_np.collect_favorite(True)
        self.assertEqual("no_track", result["action"])

    async def test_added_to_songloft_favorites(self):
        self._queue()
        calls, patches = self._patches(added=1)
        for p in patches:
            p.start()
        try:
            result = await cch_np.collect_favorite(True)
            await asyncio.sleep(0)  # 让 ensure_future 的 favfile 同步 task 跑完一轮
        finally:
            for p in patches:
                p.stop()
        self.assertEqual("added", result["action"])
        self.assertEqual([42], calls["add"])
        self.assertEqual(["R1"], calls["ensure"])
        self.assertEqual(1, calls["push"], "added 后应 fire push_favorites（票 25）")

    async def test_queue_item_is_authoritative_over_stale_current_song(self):
        """§2.77-51 时序修复：current_song.json 由 worker 15s 轮询写，点播后立即收藏
        时它停在旧歌；内存队列 _cch_state.items[entry] 点播即时更新，必须以队列为权威，
        否则收藏写错歌（真机已复现：播风吹麦浪收藏成晴天）。"""
        # 滞后的设备侧数据：旧歌《晴天》
        self._cur_song = {"name": "晴天 - 周杰伦", "id": "B3A52OLD",
                          "source": "KWQQ", "artist": "周杰伦"}
        # 准确的内存队列：当前播放《风吹麦浪》
        sr._cch_state.update({"entry": 0, "items": [{
            "id": "24A3E862", "source": "kugou", "name": "风吹麦浪",
            "artist": "李健", "album": "想念你",
            "cover": "http://imge.kugou.com/stdmusic/400/feng.jpg",
            "duration_s": 247,
        }]})
        calls, patches = self._patches(added=1)
        for p in patches:
            p.start()
        try:
            result = await cch_np.collect_favorite(True)
            await asyncio.sleep(0)  # 让 ensure_future 的 favfile 同步 task 跑完一轮
        finally:
            for p in patches:
                p.stop()
            sr._cch_state.update({"entry": 0, "items": []})
        self.assertEqual("added", result["action"])
        written = calls["ensure_items"][0]
        self.assertEqual(["24A3E862"], calls["ensure"], "必须收藏队列里的当前曲，不是滞后的晴天")
        self.assertEqual("风吹麦浪", written.get("name"))
        self.assertEqual("http://imge.kugou.com/stdmusic/400/feng.jpg", written.get("cover"))
        self.assertEqual("kugou", written.get("source"))

    async def test_cover_from_cch_queue_item(self):
        """§2.77-51：CCH 收藏以内存队列门面 item 为写入项，其原图 cover + 真实
        source 直接落库（与手机 App toRemotePayload 同格式），否则 Songloft 存空
        cover_url → 无封面。current_song.json 只有虚化图/伪源，不能作为封面源。"""
        # current_song() 返回的设备侧数据：无 cover、伪源（模拟现状）
        self._cur_song = {"name": "晴天 - 周杰伦", "id": "B3A52A7A958B",
                          "source": "KWQQ", "artist": "周杰伦"}
        # 内存队列里的门面 item：原图 cover + 真实 source
        sr._cch_state.update({"entry": 0, "items": [{
            "id": "B3A52A7A958B", "source": "kugou",
            "name": "晴天", "artist": "周杰伦", "album": "叶惠美",
            "cover": "http://imge.kugou.com/stdmusic/400/original.jpg",
            "duration_s": 269,
        }]})
        calls, patches = self._patches(added=1)
        for p in patches:
            p.start()
        try:
            result = await cch_np.collect_favorite(True)
            await asyncio.sleep(0)  # 让 ensure_future 的 favfile 同步 task 跑完一轮
        finally:
            for p in patches:
                p.stop()
            sr._cch_state.update({"entry": 0, "items": []})
        self.assertEqual("added", result["action"])
        written = calls["ensure_items"][0]
        self.assertEqual("http://imge.kugou.com/stdmusic/400/original.jpg",
                         written.get("cover"), "必须用门面原图 cover，不是虚化图")
        self.assertEqual("kugou", written.get("source"), "必须用真实平台，不是 KWQQ 伪源")

    async def test_duplicate_favorite_is_noop(self):
        self._queue()
        calls, patches = self._patches(added=0)
        for p in patches:
            p.start()
        try:
            result = await cch_np.collect_favorite(True)
            await asyncio.sleep(0)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual("noop", result["action"])
        self.assertEqual(0, calls["push"], "noop（幂等）不触发设备文件同步")

    async def test_unfavorite_removes_from_playlist(self):
        self._queue()
        calls, patches = self._patches()
        for p in patches:
            p.start()
        try:
            result = await cch_np.collect_favorite(False)
            await asyncio.sleep(0)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual("removed", result["action"])
        self.assertEqual([42], calls["remove"])
        self.assertEqual(1, calls["push"], "removed 后应 fire push_favorites（票 25）")

    async def test_songloft_failure_is_explicit(self):
        self._queue()
        calls, patches = self._patches(fail="ensure")
        for p in patches:
            p.start()
        try:
            result = await cch_np.collect_favorite(True)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual("failed", result["action"], "Songloft 不可用时不回退原厂 tap")
        self.assertEqual([], calls["add"])

    async def test_playlist_id_passthrough(self):
        """票 13 增强：指定 playlist_id 透传到 Songloft add/remove。"""
        from unittest import mock

        from core.music import songloft_api

        self._queue()
        seen = {"add": [], "remove": []}

        async def ensure(item, *, http):
            return 42

        async def add(song_id, *, http, playlist_id=None):
            seen["add"].append(playlist_id)
            return {"added": 1}

        async def remove(song_id, *, http, playlist_id=None):
            seen["remove"].append(playlist_id)
            return {"message": "ok"}

        patches = (
            mock.patch.object(songloft_api, "ensure_song", ensure),
            mock.patch.object(songloft_api, "add_to_favorites", add),
            mock.patch.object(songloft_api, "remove_from_favorites", remove),
        )
        for p in patches:
            p.start()
        try:
            r1 = await cch_np.collect_favorite(True, playlist_id=2)
            r2 = await cch_np.collect_favorite(False, playlist_id=2)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(2, r1["playlist_id"])
        self.assertEqual([2], seen["add"])
        self.assertEqual([2], seen["remove"])


class NpEnabledGateTest(unittest.TestCase):
    """票 02/11：换代编排开关必须与 cch_np_enabled 一致（真机 recorded_only 根因）。"""

    def test_np_enabled_follows_cch_np_enabled(self):
        from unittest import mock
        from core.music import cch_cast
        # cch_np_enabled=True（生产配置）→ 编排应开；旧代码额外要求不存在的 np_enabled key → 恒关
        with mock.patch.object(cch_cast, "np_enabled", lambda: True), \
             mock.patch("core.tools.song_request._cfg", lambda k, d=None: d):
            self.assertTrue(cch_np.np_enabled(),
                            "cch_np_enabled=True 时换代编排必须开（否则曲终只记录不换代）")

    def test_np_enabled_off_when_cast_off(self):
        from unittest import mock
        from core.music import cch_cast
        with mock.patch.object(cch_cast, "np_enabled", lambda: False):
            self.assertFalse(cch_np.np_enabled())


class PlaybackModeTest(unittest.TestCase):
    """票 11：单曲/批量判定 + 五模式下一首选择（纯函数，public seam）。"""

    def test_choose_next_normal_sequential(self):
        self.assertEqual(1, cch_np.choose_next("normal", 3, 0, set()))
        self.assertEqual(2, cch_np.choose_next("normal", 3, 1, set()))
        self.assertIsNone(cch_np.choose_next("normal", 3, 2, set()), "顺序末首停")

    def test_choose_next_single_once_stops_after_first(self):
        self.assertIsNone(cch_np.choose_next("single", 5, 0, set()), "单曲一次：第一首就停")
        self.assertIsNone(cch_np.choose_next("single", 5, 3, set()))

    def test_choose_next_repeatone_replays_same(self):
        self.assertEqual(0, cch_np.choose_next("repeatone", 3, 0, set()))
        self.assertEqual(2, cch_np.choose_next("repeatone", 3, 2, set()))

    def test_choose_next_repeat_wraps_to_zero(self):
        self.assertEqual(0, cch_np.choose_next("repeat", 3, 2, set()), "循环末首回队首")

    def test_choose_next_shuffle_picks_unplayed(self):
        import random
        nxt = cch_np.choose_next("shuffle", 4, 0, {0}, rng=random.Random(42))
        self.assertIn(nxt, (1, 2, 3))
        self.assertNotEqual(0, nxt, "乱序不选已播")

    def test_choose_next_shuffle_empty_pool_restarts(self):
        # 全部播过 → 重置后随机（避免死循环）
        import random
        nxt = cch_np.choose_next("shuffle", 3, 0, {0, 1, 2}, rng=random.Random(7))
        self.assertIn(nxt, (0, 1, 2))

    def test_choose_next_shuffle_rng_injection_is_deterministic(self):
        # rng 依赖注入是正当 seam（确定性测试），不是测试后门
        import random
        a = cch_np.choose_next("shuffle", 5, 0, set(), rng=random.Random(1))
        b = cch_np.choose_next("shuffle", 5, 0, set(), rng=random.Random(1))
        self.assertEqual(a, b)

    def test_choose_next_unknown_mode_falls_back_normal(self):
        self.assertEqual(1, cch_np.choose_next("unknown", 3, 0, set()))


class PlaybackModeIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """票 11：play_mode 接入 advance（验证 pure function 真被调用）。"""

    def setUp(self):
        from core.tools import song_request as sr
        from core.music import cch_cast
        self.sr = sr
        self._saved = dict(sr._cch_state)
        self._mode = cch_np.current_play_mode()
        self._cast_saved = {k: getattr(cch_cast, k) for k in (
            "revoke", "resolve_ticket_url", "kwqq_meta", "cast_native",
            "host_token_deploy", "m5_enabled", "wait_np_play")}
        cch_np._last_advance_at = -1e9

    def tearDown(self):
        from core.music import cch_cast
        self.sr._cch_state.clear()
        self.sr._cch_state.update(self._saved)
        cch_np.set_play_mode(self._mode)
        for k, v in self._cast_saved.items():
            setattr(cch_cast, k, v)

    def _fake_downlink(self):
        from unittest import mock
        cch_cast = __import__('core.music.cch_cast', fromlist=['x'])
        rec = {"revoke": [], "cast": []}
        async def fake_revoke(session, epoch, entry):
            rec["revoke"].append(entry); return True
        async def fake_resolve(ref, *, session, epoch, entry):
            return "http://ticket"
        async def fake_meta(rid, source):
            return {"cover": "", "title": "歌" + rid, "duration": 200}
        async def fake_cast(rd):
            rec["cast"].append(rd); return True, "OK"
        async def fake_token():
            pass
        cch_cast.revoke = fake_revoke
        cch_cast.resolve_ticket_url = fake_resolve
        cch_cast.kwqq_meta = fake_meta
        cch_cast.cast_native = fake_cast
        cch_cast.host_token_deploy = fake_token
        cch_cast.m5_enabled = lambda: False
        import asyncio as _aio
        async def fake_wait(t):
            return True
        cch_cast.wait_np_play = fake_wait
        return rec

    async def test_single_mode_finish_stops_no_advance(self):
        rec = self._fake_downlink()
        self.sr._cch_state.update({"entry": 0, "items": [
            {"source": "kwqq", "id": "s1"}, {"source": "kwqq", "id": "s2"}],
            "play_intent": "single", "played": []})
        action = await cch_np.on_np_event("STATE", {"statusStr": "finish", "audioStatus": "1"})
        self.assertEqual("single_once", action["action"])
        self.assertEqual(len(rec["cast"]), 0, "单曲一次：曲终不推进")
        self.assertEqual(self.sr._cch_state["entry"], 0)

    async def test_repeatone_mode_finish_replays_same(self):
        rec = self._fake_downlink()
        cch_np.set_play_mode("repeatone")
        self.sr._cch_state.update({"entry": 1, "items": [
            {"source": "kwqq", "id": "s1"}, {"source": "kwqq", "id": "s2"}],
            "play_intent": "batch", "played": []})
        action = await cch_np.on_np_event("STATE", {"statusStr": "finish", "audioStatus": "1"})
        self.assertEqual("advanced", action["action"])
        self.assertEqual(action["index"], 1, "单曲循环：仍播当前曲")

    async def test_repeat_mode_wraps_from_last(self):
        rec = self._fake_downlink()
        cch_np.set_play_mode("repeat")
        self.sr._cch_state.update({"entry": 1, "items": [
            {"source": "kwqq", "id": "s1"}, {"source": "kwqq", "id": "s2"}],
            "play_intent": "batch", "played": []})
        action = await cch_np.on_np_event("STATE", {"statusStr": "finish", "audioStatus": "1"})
        self.assertEqual("advanced", action["action"])
        self.assertEqual(action["index"], 0, "循环：末首回队首")

    def test_set_play_mode_validates(self):
        cch_np.set_play_mode("Shuffle")
        self.assertEqual("shuffle", cch_np.current_play_mode())
        cch_np.set_play_mode("BOGUS")
        self.assertEqual("shuffle", cch_np.current_play_mode(), "未知值保留上次")


if __name__ == "__main__":
    unittest.main()
