"""可选 GENA 增强（B-09）契约测试：纯 fake，不联网、不连设备。

覆盖 DoD：默认关闭；同代次同义 NOTIFY 只生效一次、真实重播不被永久去重；
SID/SEQ 的重复/乱序/缺口；到期与续订；来源/SID/正文上限/解析边界。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.music.gena import GenaGate  # noqa: E402


def _notify(state: str, uri: str = "http://nas/a.mp3") -> str:
    return (
        "<e:propertyset><e:property><LastChange>"
        f"<Event xmlns=\"urn:schemas-upnp-org:metadata-1-0/AVT/\">"
        f"<InstanceID val=\"0\"><TransportState val=\"{state}\"/>"
        f"<CurrentTrackURI val=\"{uri}\"/></InstanceID></Event>"
        "</LastChange></e:property></e:propertyset>"
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [0.0]
        self.gate = GenaGate(enabled=True, allowed_sources=("192.168.0.20",),
                             clock=lambda: self.now[0])
        self.sub = self.gate.subscribe("http://bridge/notify", timeout_s=300)

    def _accept(self, seq, body=None, sid=None, source="192.168.0.20"):
        return self.gate.accept(source=source, sid=sid or self.sub.sid, seq=seq,
                                body=body if body is not None else _notify("STOPPED"))


class DefaultOffTest(unittest.TestCase):
    def test_disabled_gate_ignores_everything(self) -> None:
        gate = GenaGate(enabled=False)
        sub = gate.subscribe("http://bridge/notify")
        result = gate.accept(source="x", sid=sub.sid, seq=1, body=_notify("STOPPED"))
        self.assertFalse(result.accepted)
        self.assertEqual("disabled", result.reason)


class SequenceTest(_Base):
    def test_duplicate_seq_is_dropped(self) -> None:
        self.assertTrue(self._accept(1).accepted)
        result = self._accept(1)
        self.assertFalse(result.accepted)
        self.assertEqual("duplicate_seq", result.reason)

    def test_stale_seq_is_dropped(self) -> None:
        self._accept(1)
        self._accept(2, body=_notify("PLAYING"))
        result = self._accept(1, body=_notify("PLAYING"))
        self.assertEqual("stale_seq", result.reason)

    def test_gap_requires_snapshot(self) -> None:
        self._accept(1)
        result = self._accept(5, body=_notify("PLAYING"))
        self.assertFalse(result.accepted)
        self.assertEqual("seq_gap", result.reason)
        self.assertTrue(result.snapshot)

    def test_bad_seq_is_rejected(self) -> None:
        self.assertEqual("bad_seq", self._accept("abc").reason)
        self.assertEqual("bad_seq", self._accept(-1).reason)


class SemanticIdempotenceTest(_Base):
    def test_same_notify_with_new_seq_applies_once(self) -> None:
        first = self._accept(1)
        second = self._accept(2)      # 同义、不同 SEQ
        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual("duplicate_semantic", second.reason)
        self.assertEqual(1, self.sub.events)

    def test_real_replay_after_new_generation_is_accepted(self) -> None:
        self._accept(1)
        self.gate.note_playback_started()      # 真实重播：新代次
        result = self._accept(2)               # 同一 URI/状态
        self.assertTrue(result.accepted, "新代次的真实重播不得被永久去重吞掉")


class ValidationTest(_Base):
    def test_unknown_sid_asks_for_resubscribe(self) -> None:
        result = self._accept(1, sid="uuid:nope")
        self.assertEqual("unknown_sid", result.reason)
        self.assertTrue(result.resubscribe)

    def test_source_allowlist(self) -> None:
        result = self._accept(1, source="10.0.0.9")
        self.assertEqual("source_rejected", result.reason)

    def test_body_limit(self) -> None:
        big = _notify("STOPPED") + "x" * 70_000
        result = self._accept(1, body=big)
        self.assertEqual("body_too_large", result.reason)

    def test_parse_boundary(self) -> None:
        self.assertEqual("parse_error", self._accept(1, body="<html>not upnp</html>").reason)


class LifecycleTest(_Base):
    def test_expiry_requires_resubscribe(self) -> None:
        self.now[0] = 301.0
        result = self._accept(1)
        self.assertEqual("sid_expired", result.reason)
        self.assertTrue(result.resubscribe)

    def test_renew_extends_and_requires_snapshot(self) -> None:
        self.now[0] = 200.0
        renewed = self.gate.renew(self.sub.sid, timeout_s=300)
        self.assertIsNotNone(renewed)
        self.assertTrue(renewed.needs_snapshot)
        self.now[0] = 400.0
        self.assertTrue(self._accept(1).accepted)   # 续订后仍有效

    def test_snapshot_taken_clears_flag(self) -> None:
        self.gate.snapshot_taken(self.sub.sid)
        self.assertFalse(self.sub.needs_snapshot)

    def test_unsubscribe(self) -> None:
        self.assertTrue(self.gate.unsubscribe(self.sub.sid))
        self.assertEqual("unknown_sid", self._accept(1).reason)


if __name__ == "__main__":
    unittest.main()
