"""点歌背景图：把封面在服务端虚化后缓存，供原厂全屏歌词页当背景。

用户口径（2026-11-21）：全屏歌词页背景应是**虚化**效果，不是清晰专辑图。
原厂云端会下发一张专门的艺术背景图（`backgroundImage`），我们没有该素材，故用
封面自身做高斯模糊生成同尺寸背景，并通过 bridge 只读端点提供给设备。

设计：
- 缓存键 = 封面 URL 的 sha1 前 16 位；文件 `<CCH_BG_DIR>/<key>.jpg`（默认 /app/data/cch-bg）。
- 模糊在 Pillow 里做：缩到宽 480 → GaussianBlur(18) → JPEG q80（几十 KB，设备拉取快）。
- 只读端点由 api_server 注册（`/api/cch/bg/<key>.jpg`），无需鉴权（键不可猜且只读）。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os

import aiohttp

from core.utils.logger import logger

DIR = os.environ.get("CCH_BG_DIR", "/app/data/cch-bg")
PREFIX = "/api/cch/bg/"
_BLUR_WIDTH = 480
_BLUR_RADIUS = 18
_sem = asyncio.Semaphore(2)


def key_for(cover_url: str) -> str:
    return hashlib.sha1(str(cover_url or "").encode("utf-8")).hexdigest()[:16]


def file_path(key: str) -> str:
    return os.path.join(DIR, key + ".jpg")


def _blur_jpeg(data: bytes) -> bytes | None:
    try:
        from PIL import Image, ImageFilter

        im = Image.open(io.BytesIO(data)).convert("RGB")
        if im.width <= 0 or im.height <= 0:
            return None
        w = min(_BLUR_WIDTH, im.width)
        h = max(1, int(im.height * w / im.width))
        im = im.resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(radius=_BLUR_RADIUS))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 - 模糊失败只降级
        logger.warning(f"[BgCache] blur failed: {type(exc).__name__}: {exc}", module="BgCache")
        return None


async def ensure(cover_url: str, *, base: str, http: aiohttp.ClientSession) -> str:
    """返回可被设备访问的虚化背景 URL；失败返回空串（调用方回退封面）。"""
    cover_url = str(cover_url or "").strip()
    if not cover_url:
        return ""
    key = key_for(cover_url)
    path = file_path(key)
    if os.path.exists(path):
        return f"{base}{PREFIX}{key}.jpg"
    async with _sem:
        if os.path.exists(path):
            return f"{base}{PREFIX}{key}.jpg"
        try:
            async with http.get(cover_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return ""
                raw = await resp.read()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BgCache] fetch failed: {type(exc).__name__}", module="BgCache")
            return ""
        jpg = await asyncio.to_thread(_blur_jpeg, raw)
        if not jpg:
            return ""
        try:
            os.makedirs(DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(jpg)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BgCache] write failed: {type(exc).__name__}", module="BgCache")
            return ""
    logger.info(f"[BgCache] generated {key}.jpg ({len(jpg)}B)", module="BgCache")
    return f"{base}{PREFIX}{key}.jpg"
