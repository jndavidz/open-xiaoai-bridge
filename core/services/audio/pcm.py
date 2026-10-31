"""Capture PCM helpers: normalize the device byte stream to int16."""

from __future__ import annotations

import numpy as np

SUPPORTED_BITS = (16, 32)


def to_int16(data: bytes, bits: int) -> bytes:
    """Convert little-endian S16/S32 capture bytes to interleaved S16."""
    if not data:
        return b""
    if bits == 16:
        return data if len(data) % 2 == 0 else data[:-1]
    if bits == 32:
        usable = len(data) - len(data) % 4
        if usable <= 0:
            return b""
        samples = np.frombuffer(data[:usable], dtype="<i4")
        return np.clip(samples >> 16, -32768, 32767).astype("<i2").tobytes()
    raise ValueError(f"unsupported capture bits: {bits}")
