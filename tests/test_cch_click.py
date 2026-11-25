"""CCH 历史点击 worker 契约测试（票 23，design §2.1/§2.6/§2.7，纯离线）。

覆盖：
- 归一化：括注后缀（「刘巧儿 (四段)」vs「刘巧儿」）、空白、大小写；
- 时间窗去重（design 主判据）：语音点歌后云端回声 ContentPlay → drop；
  窗外 / 异曲 → 放行；artist 单侧缺失按 name 判；
- 事件处理：drop 不触发播放；放行走 song_request.run(target=cch) 同一条链；
  worker 起播后自身占窗（spool 重复事件不二次起播）；
- worker 循环：clickpoll 拉取 → 逐事件处理；单事件异常不影响 worker；3 连败降频 5s；
- 云端 Pause 抢断（§2.71-3）：观察到 paused 才补一次 resume，非 paused 不动。
- 收藏点击错歌修复（§2.77-45）：FAVCLICK 后 3s 内 cp_hook 云端错歌 spool 抑制，
  窗口过期后 NE 事件正常放行。

纯离线：cch_cast.musicctl / song_request.run / cch_np.snapshot 模块属性替换，
不联网、不连设备。
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

from core.music import cch_cast  # noqa: E402
from core.music import cch_click  # noqa: E402
from core.music import cch_np  # noqa: E402
from core.tools import song_request as sr  # noqa: E402


class CchClickTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        cch_click._recent.clear()
        cch_click._recent_voice_ts = 0.0
        cch_click._recent_favclick_ts = 0.0
        cch_click._recent_playtoggle_ts = 0.0
        cch_click._pending_ev = None
        if cch_click._pending_task is not None:
            cch_click._pending_task.cancel()
        cch_click._pending_task = None
        sr._cch_state.update({"entry": 0, "items": [], "no_advance": False})
        self._saved_musicctl = cch_cast.musicctl
        self._saved_host_cmd = cch_cast.host_cmd

    def tearDown(self):
        cch_click._recent.clear()
        cch_click._recent_voice_ts = 0.0
        cch_click._recent_favclick_ts = 0.0
        cch_click._recent_playtoggle_ts = 0.0
        cch_click._pending_ev = None
        if cch_click._pending_task is not None:
            cch_click._pending_task.cancel()
        cch_click._pending_task = None
        sr._cch_state.update({"entry": 0, "items": [], "no_advance": False})
        cch_cast.musicctl = self._saved_musicctl
        cch_cast.host_cmd = self._saved_host_cmd

    # ---- _norm ----

    def test_norm_strips_bracket_suffix_whitespace_case(self):
        self.assertEqual(cch_click._norm("刘巧儿 (四段)"), cch_click._norm("刘巧儿"))
        self.assertEqual(cch_click._norm(" Liu Er "), "liuer")
        self.assertEqual(cch_click._norm("（Live）"), "")

    # ---- dedupe（8s 时间窗，design 主判据）----

    def test_dedupe_drops_echo_within_window(self):
        fake_time = mock.Mock()
        fake_time.time.return_value = 1000.0
        with mock.patch.object(cch_click, "time", fake_time), \
             mock.patch.object(cch_click, "_cfg", lambda k, d=None: 8.0):
            cch_click.note_recent_play("刘巧儿 (四段)", "新凤霞")
            self.assertNotEqual(cch_click.dedupe("刘巧儿", "新凤霞", now=1005.0), "")
            # 窗外（>8s）→ 放行
            self.assertEqual(cch_click.dedupe("刘巧儿", "新凤霞", now=1008.5), "")

    def test_dedupe_allows_different_song(self):
        cch_click.note_recent_play("晴天", "周杰伦")
        self.assertEqual(cch_click.dedupe("七里香", "周杰伦", now=cch_click.time.time()), "")

    def test_dedupe_name_match_with_empty_artist(self):
        cch_click.note_recent_play("晴天", "")
        self.assertNotEqual(cch_click.dedupe("晴天", "周杰伦", now=cch_click.time.time()), "")

    # ---- _accept_event：去抖 last-tap-wins（票 23 实测双发形态）----
    # 时序确定性原则：真 sleep + resume noop 补丁（不 patch 全局 asyncio.sleep，
    # 那会把去抖计时与测试等待压成同一毫秒产生竞态）。

    async def test_accept_single_event_plays_after_debounce(self):
        seen = {}

        async def fake_run(args):
            seen.update(args)
            return "ok"

        async def noop_resume():
            return

        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05), \
             mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_resume_if_paused", noop_resume):
            cch_click._accept_event({"name": "晴天", "artist": "周杰伦", "source": "HIST"})
            t = cch_click._pending_task
            self.assertIsNotNone(t)
            await asyncio.wait_for(t, timeout=2)
        self.assertEqual(seen.get("query"), "晴天")
        self.assertEqual(seen.get("target"), "cch")

    async def test_accept_cp_hook_ne_event_plays_after_debounce(self):
        """当前播放 tab 点击：cp_hook 在 :core 拦云端 ContentPlay 提取的 NE 事件，
        无主动点播登记时须放行点播（票 23 的无条件 drop 作废）。"""
        seen = {}

        async def fake_run(args):
            seen.update(args)
            return "ok"

        async def noop_resume():
            return

        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05), \
             mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_resume_if_paused", noop_resume):
            cch_click._accept_event({"name": "无愧我心", "artist": "成龙",
                                     "source": "NE", "rid": "E4340B4A"})
            t = cch_click._pending_task
            self.assertIsNotNone(t)
            await asyncio.wait_for(t, timeout=2)
        self.assertEqual(seen.get("query"), "无愧我心")
        self.assertEqual(seen.get("target"), "cch")

    def test_cp_hook_echo_dropped_when_recent_play_registered(self):
        """主动点播登记的曲目在 8s 窗内的 cp_hook 事件（回声）须被 dedupe 丢弃。"""
        cch_click.note_recent_play("晴天", "周杰伦")
        # 复用现有 dedupe 用例的口径：同名同歌手窗内 → 非空原因
        self.assertNotEqual(cch_click.dedupe("晴天", "周杰伦", now=cch_click.time.time()), "")
        self.assertIsNone(cch_click._pending_ev)

    async def test_accept_two_events_last_tap_wins(self):
        played = []

        async def fake_run(args):
            played.append(args.get("query"))
            return "ok"

        async def noop_resume():
            return

        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.2), \
             mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_resume_if_paused", noop_resume):
            cch_click._accept_event({"name": "刘巧儿", "artist": "", "source": "HIST"})
            t1 = cch_click._pending_task
            await asyncio.sleep(0.05)    # echo 先到；debounce=0.2s 未到期
            cch_click._accept_event({"name": "七里香", "artist": "", "source": "HIST"})
            t2 = cch_click._pending_task
            self.assertIsNot(t1, t2, "新事件必须替换未决任务")
            await asyncio.wait_for(t2, timeout=2)   # 确定性等最后一条播完
        self.assertEqual(played, ["七里香"], "回声被替换，只播最后一条（刘巧儿未播）")

    async def test_play_event_sets_no_advance_keeps_items(self):
        """点击播 = 单曲意图：置 no_advance 换代抑制；items 保留（收藏读当前曲）。"""
        sr._cch_state.update({"entry": 0, "items": [{"id": "x", "name": "刘巧儿"}],
                              "no_advance": False})

        async def fake_run(args):
            return "ok"

        async def noop_resume():
            return

        with mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_resume_if_paused", noop_resume):
            await cch_click._play_event(
                {"name": "晴天", "artist": "", "source": "HIST"})
        self.assertEqual(sr._cch_state.get("no_advance"), True)
        self.assertEqual(len(sr._cch_state.get("items") or []), 1, "items 保留供收藏")

    # ---- _play_event ----

    async def test_play_event_drops_on_recent_play(self):
        cch_click.note_recent_play("晴天", "周杰伦")
        called = []

        async def fake_run(args):
            called.append(args)
            return "ok"

        with mock.patch("core.tools.song_request.run", fake_run):
            await cch_click._play_event(
                {"name": "晴天", "artist": "周杰伦", "source": "KWQQ", "rid": ""})
        self.assertEqual(called, [], "echo 事件不得触发播放")

    async def test_play_event_plays_via_song_request_cch_chain(self):
        seen = {}

        async def fake_run(args):
            seen.update(args)
            return "已在 CCH 上为你播放《晴天》"

        real_sleep = asyncio.sleep

        async def quick_sleep(d, *a, **k):   # resume 补偿任务的 1.5s 等待即时完成
            await real_sleep(0.001)

        with mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(asyncio, "sleep", quick_sleep):
            await cch_click._play_event(
                {"name": "晴天", "artist": "周杰伦", "source": "KWQQ", "rid": "cloud-1"})
            await real_sleep(0.02)           # 让 fire-and-forget 的 resume 任务跑完
        self.assertEqual(seen.get("target"), "cch")
        self.assertEqual(seen.get("action"), "play")
        self.assertEqual(seen.get("query"), "晴天")
        self.assertEqual(seen.get("artist"), "周杰伦")
        # worker 自身起播也占窗：同一事件重复投递（CP+createlist 双发）被去重
        self.assertNotEqual(cch_click.dedupe("晴天", "周杰伦"), "")

    def test_clean_fav_name(self):
        """收藏栏标题仅做展示清洗（剥纯歌名），不承担平台传递（§2.77-57）。"""
        cases = [
            ("夜来香-《熱唱！東京夜景》-咪咕", "夜来香"),
            ("恰似你的温柔-《华语经典好歌珍藏》-酷我", "恰似你的温柔"),
            ("晴天-《叶惠美》-酷狗", "晴天"),
            ("晴天", "晴天"),                     # 无后缀原样
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(expected, cch_click.clean_fav_name(raw))

    async def test_favclick_passes_rawid_source_to_song_request(self):
        """FAVCLICK spool 带 rid(rawID)+album(平台) → 直传 song_request，跳过搜索（§2.77-57）。"""
        seen = {}

        async def fake_run(args):
            seen.update(args)
            return "ok"

        with mock.patch("core.tools.song_request.run", fake_run):
            await cch_click._play_event({
                "name": "夜来香-《熱唱！東京夜景》-咪咕", "artist": "",
                "source": "FAVCLICK", "rid": "600919000005273800", "album": "MIGU"})

        self.assertEqual("600919000005273800", seen.get("raw_id"), "rawID 透传")
        self.assertEqual("migu", seen.get("source"), "平台 lower 后透传")
        self.assertEqual("夜来香", seen.get("query"), "纯歌名仅用于播报/兜底")

    async def test_curplay_click_passes_rawid_source(self):
        """票 12：当前播放 tab 点击 CLICK spool 带 rid(rawID)+album(平台) → rawID 直构点播，
        不依赖云端错歌名（真机：Cloud NE=荒原，我方队列曲 rawId=7D6B... 喜月）。"""
        seen = {}

        async def fake_run(args):
            seen.update(args)
            return "ok"

        with mock.patch("core.tools.song_request.run", fake_run):
            await cch_click._play_event({
                "name": "喜月 - 任素汐", "artist": "任素汐",
                "source": "CLICK", "rid": "7D6BF0230D0486BD7981D72AA6586E6B",
                "album": "KWQQ"})

        self.assertEqual("7D6BF0230D0486BD7981D72AA6586E6B", seen.get("raw_id"),
                         "rawID 透传，不用云端错歌名重搜")
        self.assertEqual("kwqq", seen.get("source"))

    async def test_histpos_resolves_truth_source_by_pos(self):
        """HISTPOS(pos) → cch_history 反查 (rawID, source, name) → rawID 直构点播。
        架构统一：显示索引只是定位，歌曲身份永远是 (source, rawID)，不用歌名搜索。"""
        from core.music import cch_history

        seen = {}

        async def fake_run(args):
            seen.update(args)
            return "ok"

        truth = {"rawId": "671381931", "source": "kuwo", "name": "恰似你的温柔", "artist": "邓丽君"}
        with mock.patch.object(cch_history, "get_by_pos", new_callable=mock.AsyncMock,
                               return_value=truth), \
             mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch("core.music.cch_cast.push_favorites", new_callable=mock.AsyncMock):
            await cch_click._histpos_play({"source": "HISTPOS", "rid": "4", "name": "histpos"})

        self.assertEqual("671381931", seen.get("raw_id"), "按 rawID 直构，不用歌名")
        self.assertEqual("kuwo", seen.get("source"))
        self.assertEqual("恰似你的温柔", seen.get("query"), "歌名仅作播报")

    async def test_histpos_past_end_skips(self):
        """pos 越界 → 反查失败 → 不点播（fail-closed）。"""
        from core.music import cch_history

        with mock.patch.object(cch_history, "get_by_pos", new_callable=mock.AsyncMock,
                               return_value=None), \
             mock.patch("core.tools.song_request.run", new_callable=mock.AsyncMock) as run, \
             mock.patch("core.music.cch_cast.push_favorites", new_callable=mock.AsyncMock):
            await cch_click._histpos_play({"source": "HISTPOS", "rid": "99", "name": "histpos"})
        run.assert_not_awaited()

    async def test_favclick_without_rawid_falls_back(self):
        """旧 so / 解析异常无真相源字段 → 回退按歌名重搜（fail-open，不比修复前差）。"""
        seen = {}

        async def fake_run(args):
            seen.update(args)
            return "ok"

        with mock.patch("core.tools.song_request.run", fake_run):
            await cch_click._play_event({
                "name": "晴天", "artist": "", "source": "FAVCLICK"})

        self.assertNotIn("raw_id", seen)
        self.assertEqual("晴天", seen.get("query"))

    # ---- worker 循环 ----

    async def test_worker_polls_clickpoll_and_plays(self):
        calls = []

        async def fake_musicctl(op, body=None):
            calls.append((op, body))
            return True, {"events": [{"name": "歌一", "artist": "", "source": "HIST"}],
                          "dropped": 0}

        played = []

        async def fake_run(args):
            played.append(args.get("query"))
            return "ok"

        with mock.patch.object(cch_cast, "musicctl", fake_musicctl), \
             mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05):
            task = asyncio.create_task(cch_click.run_worker())
            for _ in range(50):          # 确定性等 debounce+play 完成
                if played:
                    break
                await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(any(op == "clickpoll" for op, _ in calls))
        self.assertEqual(played, ["歌一"])

    async def test_worker_survives_event_and_poll_errors(self):
        state = {"n": 0}

        async def flaky_musicctl(op, body=None):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("boom")
            return True, {"events": [], "dropped": 0}

        played = []

        async def fake_run2(args):
            played.append(args.get("query"))
            return "ok"

        real_sleep = asyncio.sleep

        async def quick_sleep(d, *a, **k):
            await real_sleep(0.001)

        with mock.patch.object(cch_cast, "musicctl", flaky_musicctl), \
             mock.patch("core.tools.song_request.run", fake_run2), \
             mock.patch.object(asyncio, "sleep", quick_sleep):
            task = asyncio.create_task(cch_click.run_worker())
            await real_sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(state["n"] >= 2, "poll 失败后继续轮询")

    async def test_hist_event_beats_later_same_song_echo(self):
        """票 23 真机 16:58 同毫秒双事件：HIST(晴天) + 云端回声(晴天) 后到
        → dedupe（同曲）挡回声，只播 HIST 的晴天。历史 tab 有 HIST 覆盖真意图，
        当前播放 tab 无 HIST 才靠 cp_hook 事件放行——两者都走 dedupe 是统一地。"""
        played = []

        async def fake_run(args):
            played.append(args.get("query"))
            return "ok"

        async def noop_resume():
            return

        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05), \
             mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_resume_if_paused", noop_resume):
            cch_click._accept_event({"name": "晴天", "artist": "", "source": "HIST"})
            # 同曲云端回声（HIST 意图起播后 note_recent_play 会登记晴天）
            cch_click.note_recent_play("晴天", "周杰伦")
            cch_click._accept_event({"name": "晴天", "artist": "", "source": "KG"})
            t = cch_click._pending_task
            self.assertIsNotNone(t)
            await asyncio.wait_for(t, timeout=2)
        self.assertEqual(played, ["晴天"], "同曲 KG 回声不得顶掉 HIST 意图")

    async def test_recent_play_echo_dropped_by_dedupe(self):
        """主动点播（语音/song_request）登记的曲目，8s 窗内 cp_hook 的 KG 回声
        → dedupe 丢弃，不与语音链竞速（票 23 的原诉求，改由 dedupe 而非源过滤实现）。"""
        cch_click.note_recent_play("亲爱的小孩", "")
        result = cch_click.dedupe("亲爱的小孩", "", now=cch_click.time.time())
        self.assertNotEqual(result, "", "窗内同曲回声须被 dedupe 丢弃")

    async def test_worker_backs_off_after_3_failures(self):
        state = {"n": 0}

        async def flaky_musicctl(op, body=None):
            state["n"] += 1
            if state["n"] <= 3:
                raise RuntimeError("boom")
            return True, {"events": [], "dropped": 0}

        sleeps = []
        real_sleep = asyncio.sleep

        async def fake_sleep(d, *a, **k):
            sleeps.append(d)
            await real_sleep(0.001)

        with mock.patch.object(cch_cast, "musicctl", flaky_musicctl), \
             mock.patch.object(asyncio, "sleep", fake_sleep):
            task = asyncio.create_task(cch_click.run_worker())
            await real_sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(sleeps[:3], [cch_click._POLL_S, cch_click._POLL_S, cch_click._SLOW_POLL_S],
                         "3 连败后第 4 次等待降到 5s")

    # ---- 云端 Pause 抢断（§2.71-3）----

    async def test_resume_only_on_observed_pause(self):
        resumes = []

        async def fake_host_cmd(cmd, url=None, extra=None, timeout_s=5.0):
            resumes.append(cmd)
            return True, "OK"

        st_paused = {"state": {"extras": {"statusStr": "pause"}}}
        st_playing = {"state": {"extras": {"statusStr": "play"}}}
        real_sleep = asyncio.sleep

        async def quick_sleep(d, *a, **k):   # 跳过 1.5s 真等待
            await real_sleep(0.001)

        with mock.patch.object(cch_cast, "host_cmd", fake_host_cmd), \
             mock.patch.object(cch_np, "snapshot", return_value=st_paused), \
             mock.patch.object(asyncio, "sleep", quick_sleep):
            await cch_click._resume_if_paused()
        self.assertEqual(resumes, ["resume"], "观察到 paused 才补一次 resume")

        resumes.clear()
        with mock.patch.object(cch_cast, "host_cmd", fake_host_cmd), \
             mock.patch.object(cch_np, "snapshot", return_value=st_playing), \
             mock.patch.object(asyncio, "sleep", quick_sleep):
            await cch_click._resume_if_paused()
        self.assertEqual(resumes, [], "playing 不动")

    async def test_voice_guard_drops_cp_event_when_voice_recent(self):
        """2026-11-23：近期有 external text（语音应答）时，cp_hook 的 NE 事件被忽略。"""
        played = []

        async def fake_run(args):
            played.append(args.get("query"))
            return "ok"

        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05), \
             mock.patch("core.tools.song_request.run", fake_run):
            cch_click.note_voice_input()
            cch_click._accept_event({"name": "发如雪", "artist": "周杰伦", "source": "NE"})
            self.assertIsNone(cch_click._pending_task)
        self.assertEqual(played, [], "近期语音时 cp_hook 事件须被 voice-guard 丢弃")

    async def test_voice_guard_allows_cp_event_without_voice(self):
        """无近期语音（触屏点击）时，cp_hook 的 NE 事件照常放行。"""
        played = []

        async def fake_run(args):
            played.append(args.get("query"))
            return "ok"

        async def noop_resume():
            return

        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05), \
             mock.patch("core.tools.song_request.run", fake_run), \
             mock.patch.object(cch_click, "_resume_if_paused", noop_resume):
            cch_click._accept_event({"name": "发如雪", "artist": "周杰伦", "source": "NE"})
            t = cch_click._pending_task
            self.assertIsNotNone(t)
            await asyncio.wait_for(t, timeout=2)
        self.assertEqual(played, ["发如雪"], "无近期语音时 cp_hook 事件须放行")


    async def test_favclick_guard_drops_cloud_echo(self):
        """2026-11-24 §2.77-45：FAVCLICK 后短时间内的 cp_hook NE 云端错歌 spool 被丢弃。"""
        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05), \
             mock.patch("core.tools.song_request.run", mock.AsyncMock(return_value="ok")):
            cch_click._accept_event({"name": "爱你一万年", "artist": "邓丽君", "source": "FAVCLICK"})
            self.assertIsNotNone(cch_click._recent_favclick_ts)
            # 随后 cp_hook 云端错歌 spool（同一次点击）→ favclick-guard drop
            cch_click._accept_event({"name": "金刚经", "artist": "王菲", "source": "NE"})
            self.assertIsNotNone(cch_click._pending_ev)
            self.assertEqual(cch_click._pending_ev.get("name"), "爱你一万年",
                             "错歌须被 drop，pending 仍是 FAVCLICK 的正确曲")
            if cch_click._pending_task is not None:
                cch_click._pending_task.cancel()

    async def test_favclick_guard_expired_no_suppression(self):
        """FAVCLICK 时间戳过期后，NE 事件不再被抑制（经 dedupe 正常判定）。"""
        import time
        cch_click._recent_favclick_ts = time.time() - 10  # 远超 3s guard
        # 直接调 _accept_event：NE 事件不被 favclick-guard drop（会进 dedupe/debounce）
        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05), \
             mock.patch("core.tools.song_request.run", mock.AsyncMock(return_value="ok")):
            cch_click._accept_event({"name": "金刚经", "artist": "王菲", "source": "NE"})
            self.assertIsNotNone(cch_click._pending_ev)
            self.assertEqual(cch_click._pending_ev.get("name"), "金刚经",
                             "窗口外 NE 不被 favclick-guard 抑制")
            if cch_click._pending_task is not None:
                cch_click._pending_task.cancel()


    async def test_playtoggle_in_stopped_state_replays_current(self):
        """票 06：曲终停止态（STATE finish/stop）点播放键 → 重播当前曲，不做原子 toggle
        （native 实例已死，toggle→resume 无效）。"""
        from unittest import mock
        from core.music import cch_np as cch_np_mod

        replay_calls = []
        toggle_calls = []

        async def fake_replay():
            replay_calls.append(True)
            return {"action": "replayed", "ok": True}

        async def fake_toggle(cmd):
            toggle_calls.append(cmd)
            return True, "OK"

        with mock.patch.object(cch_np_mod, "snapshot",
                               lambda: {"state": {"extras": {"statusStr": "finish"}}}), \
             mock.patch.object(cch_np_mod, "replay_current", fake_replay), \
             mock.patch("core.music.cch_cast.host_cmd", fake_toggle):
            await cch_click._playtoggle({"source": "PLAYTOGGLE"})

        self.assertEqual(1, len(replay_calls), "stopped 态应重播当前曲")
        self.assertEqual([], toggle_calls, "stopped 态不应走 toggle（对死实例无效）")

    async def test_playtoggle_in_playing_state_still_toggles(self):
        """非 stopped 态保持原语义（M5 原子 toggle）。"""
        from unittest import mock
        from core.music import cch_np as cch_np_mod

        toggle_calls = []

        async def fake_toggle(cmd):
            toggle_calls.append(cmd)
            return True, "OK"

        with mock.patch.object(cch_np_mod, "snapshot",
                               lambda: {"state": {"extras": {"statusStr": "play"}}}), \
             mock.patch("core.music.cch_cast.host_cmd", fake_toggle):
            await cch_click._playtoggle({"source": "PLAYTOGGLE"})

        self.assertEqual(["toggle"], toggle_calls)

    async def test_curplay_click_skips_dedupe_after_same_song_voice(self):
        """票 12：语音点播占窗后点当前播放 tab 同名曲，CLICK 不被 dedupe 误杀
        （review 抓的入口/_play_event 矛盾守门）。"""
        played = []

        async def fake_run(args):
            played.append(args.get("query"))
            return "ok"

        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05), \
             mock.patch("core.tools.song_request.run", fake_run):
            cch_click.note_recent_play("喜月", "任素汐")  # 近期语音点播占窗
            cch_click._accept_event({"name": "喜月 - 任素汐", "artist": "任素汐",
                                     "source": "CLICK", "rid": "7D6B", "album": "KWQQ"})
            self.assertIsNotNone(cch_click._pending_task,
                                 "CLICK 不应被同名语音点播 dedupe 误杀")
            if cch_click._pending_task is not None:
                cch_click._pending_task.cancel()

    async def test_favclick_skips_dedupe_after_same_song_voice(self):
        """§2.77-45：语音点歌后 8s 内点同名收藏曲，FAVCLICK 不被 dedupe 误杀。"""
        played = []

        async def fake_run(args):
            played.append(args.get("query"))
            return "ok"

        with mock.patch.object(cch_click, "_cfg", lambda k, d=None: 0.05), \
             mock.patch("core.tools.song_request.run", fake_run):
            cch_click.note_recent_play("爱你一万年", "邓丽君")  # 模拟近期语音点播
            cch_click._accept_event({"name": "爱你一万年", "artist": "", "source": "FAVCLICK"})
            task = cch_click._pending_task
            self.assertIsNotNone(task, "FAVCLICK 不应被同名语音点播 dedupe 误杀")
            await asyncio.wait_for(task, timeout=2)
        self.assertEqual(played, ["爱你一万年"], "FAVCLICK 正确点播")


    async def test_playtoggle_sends_toggle_cmd(self):
        """§2.77 四问题①：播放键 → host_cmd("toggle")（设备侧原子翻转，桥侧零状态读取）。"""
        calls = []

        async def fake_host_cmd(cmd):
            calls.append(cmd)
            return True, "OK"

        with mock.patch("core.music.cch_cast.host_cmd", fake_host_cmd), \
             mock.patch("core.music.cch_click.logger"):
            await cch_click._playtoggle({"source": "PLAYTOGGLE"})
        self.assertEqual(calls, ["toggle"], "必须下发设备侧 toggle 命令")

    async def test_playtoggle_debounce_drops_duplicate(self):
        """同一次点击两条 PLAYTOGGLE spool（两种 tag）→ 1s 内第二条丢弃，只调一次 _playtoggle。"""
        cch_click._recent_playtoggle_ts = 0.0
        with mock.patch.object(cch_click, "_playtoggle", new_callable=mock.AsyncMock) as pt, \
             mock.patch("core.music.cch_click.logger"):
            cch_click._accept_event({"source": "PLAYTOGGLE"})
            cch_click._accept_event({"source": "PLAYTOGGLE"})  # 立即第二条 → 去抖 drop
            await asyncio.sleep(0)
        self.assertEqual(pt.await_count, 1, "两条 spool 只触发一次 _playtoggle")

    def test_playtoggle_uses_host_cmd_toggle(self):
        """守门：playing 态只发 host_cmd("toggle")；禁读设备实时态（get_speaker /
        current_song，防双链竞速）。票 06 起允许读 bridge 自己的编排内存态
        cch_np.snapshot()（worker 上报的 np 事件记录，非设备探测）判 stopped 态，
        并走 replay_current 重播——这是与换代共用的唯一重播 seam。"""
        import inspect
        src = inspect.getsource(cch_click._playtoggle)
        self.assertIn('host_cmd("toggle")', src, "playing 态必须调 host_cmd('toggle')")
        self.assertNotIn("get_speaker", src, "禁止 LX06 speaker（设备实时态）")
        self.assertNotIn("current_song", src, "禁止桥侧读设备当前曲文件")
        self.assertIn("cch_np.snapshot()", src, "stopped 判据来自 bridge 编排态")
        self.assertIn("cch_np.replay_current()", src, "stopped 态走唯一重播 seam")

    # ---- 收藏 tab 红心点击（2026-11-25 FAVADD/FAVREMOVE）----

    async def test_fav_remove_by_rawid(self):
        """FAVREMOVE 按 rawID 反查 song_id → 统一写入口 cch_np.apply_favorite(intent=False)。"""
        from core.music import cch_np
        hit = {"song_id": 4199, "title": "恰似你的温柔", "cover": "", "album": "",
               "artist": "", "copyrightId": "", "source": "kuwo"}
        calls = []

        async def fake_apply(item, intent, **kw):
            calls.append(("apply", intent, kw.get("song_id")))
            return {"action": "removed"}

        with mock.patch("core.music.songloft_api.lookup_favorite_by_rawid",
                        new_callable=mock.AsyncMock, return_value=hit), \
             mock.patch.object(cch_np, "apply_favorite", fake_apply), \
             mock.patch("core.music.cch_click.logger"):
            await cch_click._fav_collect({"source": "FAVREMOVE", "rid": "671381931"})
        self.assertIn(("apply", False, 4199), calls)

    async def test_fav_add_by_rawid(self):
        """FAVADD 按 rawID 反查 song_id → 统一写入口 cch_np.apply_favorite(intent=True)。"""
        from core.music import cch_np
        hit = {"song_id": 4199, "title": "恰似你的温柔", "cover": "", "album": "",
               "artist": "", "copyrightId": "", "source": "kuwo"}
        calls = []

        async def fake_apply(item, intent, **kw):
            calls.append(("apply", intent, kw.get("song_id")))
            return {"action": "added"}

        with mock.patch("core.music.songloft_api.lookup_favorite_by_rawid",
                        new_callable=mock.AsyncMock, return_value=hit), \
             mock.patch.object(cch_np, "apply_favorite", fake_apply), \
             mock.patch("core.music.cch_click.logger"):
            await cch_click._fav_collect({"source": "FAVADD", "rid": "671381931"})
        self.assertIn(("apply", True, 4199), calls)

    async def test_fav_add_new_song_via_history_not_favorites(self):
        """票01 根因守门：FAVADD 新歌不在收藏表 → 从历史反查 item 走 ensure_song，
        不再因 lookup_favorite_by_rawid 返回 None 而 skip。"""
        from core.music import cch_np
        # 用真实原厂 schema 结构（authors[0].title + cover.img），不臆造顶层 artist
        hist_item = {"rawId": "B3A52A7A", "source": "KUGOU", "title": "晴天",
                     "authors": [{"title": "周杰伦"}], "duration": 269,
                     "cover": {"img": "http://img/q.jpg"},
                     "extra": {"copyrightId": "MIGU-CP-123"}}
        calls = []

        async def fake_apply(item, intent, **kw):
            calls.append(("apply", intent, kw.get("song_id"), item))
            return {"action": "added"}

        with mock.patch("core.music.songloft_api.lookup_favorite_by_rawid",
                        new_callable=mock.AsyncMock, return_value=None), \
             mock.patch("core.music.cch_history.get_by_rawid",
                        new_callable=mock.AsyncMock, return_value=hist_item), \
             mock.patch.object(cch_np, "apply_favorite", fake_apply), \
             mock.patch("core.music.cch_click.logger"):
            await cch_click._fav_collect({"source": "FAVADD", "rid": "B3A52A7A"})
        self.assertEqual(1, len(calls), "FAVADD 新歌不得 skip，必须走 apply_favorite")
        _, intent, song_id, item = calls[0]
        self.assertTrue(intent)
        self.assertIsNone(song_id, "新歌须走 ensure_song（不传 song_id）")
        self.assertEqual("B3A52A7A", item["id"])
        self.assertEqual("晴天", item["name"])
        self.assertEqual("kugou", item["source"])
        self.assertEqual("周杰伦", item["artist"])          # authors[0].title 还原
        self.assertEqual("http://img/q.jpg", item["cover"])  # cover.img 还原
        self.assertEqual("MIGU-CP-123", item["extra"]["copyrightId"])  # §2.77-58 咪咕取链

    async def test_fav_add_idempotent_second_add_is_noop(self):
        """票01 验收③幂等：二次 FAVADD 命中收藏表 → 传 song_id 走 add（added=0→noop），
        不重新 ensure_song、不产生重复收藏。"""
        from core.music import cch_np
        hit = {"song_id": 4199, "title": "晴天", "source": "kugou"}
        calls = []

        async def fake_apply(item, intent, **kw):
            calls.append((intent, kw.get("song_id")))
            return {"action": "noop"}  # Songloft added=0

        with mock.patch("core.music.songloft_api.lookup_favorite_by_rawid",
                        new_callable=mock.AsyncMock, return_value=hit), \
             mock.patch("core.music.cch_history.get_by_rawid",
                        new_callable=mock.AsyncMock) as hist, \
             mock.patch.object(cch_np, "apply_favorite", fake_apply), \
             mock.patch("core.music.cch_click.logger"):
            await cch_click._fav_collect({"source": "FAVADD", "rid": "B3A52A7A"})
            await cch_click._fav_collect({"source": "FAVADD", "rid": "B3A52A7A"})
        self.assertEqual([(True, 4199), (True, 4199)], calls)
        hist.assert_not_awaited()  # 已在收藏表 → 不走历史 ensure_song

    async def test_fav_add_no_history_still_skips(self):
        """守门：新歌既不在收藏表也不在历史 → fail-open skip（不臆造 item）。"""
        from core.music import cch_np
        with mock.patch("core.music.songloft_api.lookup_favorite_by_rawid",
                        new_callable=mock.AsyncMock, return_value=None), \
             mock.patch("core.music.cch_history.get_by_rawid",
                        new_callable=mock.AsyncMock, return_value=None), \
             mock.patch.object(cch_np, "apply_favorite", new_callable=mock.AsyncMock) as apply, \
             mock.patch("core.music.cch_click.logger"):
            await cch_click._fav_collect({"source": "FAVADD", "rid": "UNKNOWN"})
        apply.assert_not_awaited()

    async def test_fav_routes_without_pointplay(self):
        """守门：FAVADD/FAVREMOVE 在 _accept_event 里直接走 _fav_collect，不进点播去抖链。"""
        with mock.patch.object(cch_click, "_fav_collect", new_callable=mock.AsyncMock) as fav, \
             mock.patch.object(cch_click, "_play_event", new_callable=mock.AsyncMock) as play, \
             mock.patch("core.music.cch_click.logger"):
            cch_click._accept_event({"source": "FAVREMOVE", "rid": "671381931"})
            cch_click._accept_event({"source": "FAVADD", "rid": "8C655F"})
            await asyncio.sleep(0)
        fav.assert_awaited()
        self.assertEqual(fav.await_count, 2)
        play.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
