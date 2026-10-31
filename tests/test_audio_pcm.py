"""S16/S32 capture normalization tests (no device, no models)."""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio.pcm import to_int16  # noqa: E402


class ToInt16Test(unittest.TestCase):
    def test_s16_passthrough(self):
        data = np.array([1, -2, 32767, -32768], dtype="<i2").tobytes()
        self.assertEqual(data, to_int16(data, 16))

    def test_s16_odd_tail_is_dropped(self):
        data = b"\x01\x02\x03"
        self.assertEqual(b"\x01\x02", to_int16(data, 16))

    def test_s32_left_justified_is_shifted(self):
        samples = np.array(
            [0, 1 << 16, -(1 << 16), (12345 << 16), (1 << 31) - 1],
            dtype="<i4",
        )
        expected = np.array([0, 1, -1, 12345, 32767], dtype="<i2").tobytes()
        self.assertEqual(expected, to_int16(samples.tobytes(), 32))

    def test_s32_odd_tail_is_dropped(self):
        data = b"\x00\x00\x00\x00\x01\x02"
        self.assertEqual(b"\x00\x00", to_int16(data, 32))

    def test_unsupported_bits_raise(self):
        with self.assertRaises(ValueError):
            to_int16(b"\x00\x00", 24)


if __name__ == "__main__":
    unittest.main()
