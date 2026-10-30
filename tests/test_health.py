"""`/api/health` 组装契约（纯函数，ticket 08）。"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# api_server 在导入期需要 native 扩展；本测试只验证正则常量，用空模块占位。
sys.modules.setdefault("open_xiaoai_server", types.ModuleType("open_xiaoai_server"))

from core.services.health import (  # noqa: E402
    build_health_payload,
    livez_payload,
    readyz_payload,
)


class HealthPayloadTest(unittest.TestCase):
    def test_memory_sections_are_always_present(self) -> None:
        payload = build_health_payload(
            speaker_ready=True,
            audio={"frame_count": 12, "channel_strategy": "strongest"},
            playback={"source": "context", "mismatch_count": 0},
            inject={"enabled": False},
        )
        self.assertTrue(payload["success"])
        data = payload["data"]
        self.assertEqual(data["status"], "healthy")
        self.assertTrue(data["speaker_ready"])
        self.assertEqual(data["audio"]["frame_count"], 12)
        self.assertEqual(data["playback"]["source"], "context")
        self.assertFalse(data["inject"]["enabled"])
        self.assertNotIn("probe", data)

    def test_probe_is_optional_and_copied(self) -> None:
        probe = {"mediaplayer_alive": True, "memory_free": 4096, "mic": "on"}
        payload = build_health_payload(
            speaker_ready=True, probe=probe
        )
        self.assertEqual(payload["data"]["probe"], probe)
        self.assertIsNot(payload["data"]["probe"], probe)

    def test_missing_sections_default_to_empty_dicts(self) -> None:
        payload = build_health_payload(speaker_ready=False)
        data = payload["data"]
        self.assertEqual(data["audio"], {})
        self.assertEqual(data["playback"], {})
        self.assertEqual(data["inject"], {})

    def test_no_secret_shaped_keys(self) -> None:
        payload = build_health_payload(
            speaker_ready=True,
            inject={"enabled": True, "gate": "closed"},
            probe={"mic": "on"},
        )
        rendered = str(payload).lower()
        for forbidden in ("token", "password", "authorization", "cookie"):
            self.assertNotIn(forbidden, rendered)


class LivenessReadinessTest(unittest.TestCase):
    def test_livez_ignores_device(self) -> None:
        self.assertEqual(livez_payload()["data"]["status"], "alive")

    def test_readyz_ready(self) -> None:
        status, payload = readyz_payload(speaker_ready=True, recording_expected=True)
        self.assertEqual((status, payload["data"]["status"]), (200, "ready"))
        self.assertEqual(payload["data"]["reasons"], [])

    def test_readyz_speaker_missing(self) -> None:
        status, payload = readyz_payload(speaker_ready=False)
        self.assertEqual(status, 503)
        self.assertIn("speaker_unavailable", payload["data"]["reasons"])

    def test_readyz_listening_stopped(self) -> None:
        status, payload = readyz_payload(speaker_ready=True, recording_expected=False)
        self.assertEqual(status, 503)
        self.assertEqual(payload["data"]["reasons"], ["listening_stopped"])


class TraceparentPatternTest(unittest.TestCase):
    def test_w3c_traceparent_pattern(self) -> None:
        from core.services.api_server import APIServer

        pat = APIServer._TRACEPARENT_RE
        self.assertTrue(pat.match("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"))
        self.assertFalse(pat.match("00-xyz"))
        self.assertFalse(pat.match(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
