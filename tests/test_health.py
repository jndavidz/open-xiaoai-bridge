"""`/api/health` 组装契约（纯函数，ticket 08）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.health import build_health_payload  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
