"""点歌背景图：把封面在服务端虚化后缓存，供原厂全屏歌词页当背景。

用户口径（2026-11-21）：全屏歌词页背景应是**虚化**效果，不是清晰专辑图。
原厂云端会下发一张专门的艺术背景图（`backgroundImage`），我们没有该素材，故用
封面自身做高斯模糊生成同尺寸背景，并通过 bridge 只读端点提供给设备。
2026-11-23：模糊后叠**自适应黑罩**（`_darken`）——原厂 LrcView 白色歌词在浅色封面
虚化后看不清（真机截图 VERIFIED，红图验证原厂直接渲染本图）。深色封面少叠保氛围，
浅色封面多叠保证白字可读（输出亮度 <=~105）。缓存 key 加 `_v2` 强制旧图重生。

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

# 全屏歌词背景暗化（2026-11-23）：原厂 LrcView 用白色歌词，浅色封面虚化后近白 ⇒ 白字看不清。
# 原厂直接渲染我们下发的 backgroundImage（红图真机验证），无白渐变抵消 ⇒ 在虚化图里叠黑罩即可。
# 目标：白字可读需输出亮度 <= ~105；深色封面少叠保氛围。
_DARK_MIN = 0.0     # L<=50 不叠
_DARK_MAX = 0.62    # 纯白最多叠 62% 黑 ⇒ 255*0.38≈97（白字清晰）
_BG_VERSION = "v2"  # 缓存 key 版本：算法变更后强制旧图失效重生


def _overlay_alpha(mean_luma: float) -> float:
    """平均亮度 L(0..255) → 叠黑 alpha（单调不减，clamp 到 [0,0.62]）。
    L=50→0；L=128→≈0.33；L=255→0.62。白 255*(1-0.62)≈97 <=105 白字可读。"""
    a = (float(mean_luma) - 50.0) / 205.0 * _DARK_MAX
    return max(_DARK_MIN, min(_DARK_MAX, a))


def _darken(im, ImageStat):
    """对虚化图叠自适应黑罩：算平均亮度 → alpha → Image.blend 到纯黑。"""
    try:
        mean = ImageStat.Stat(im.convert("L")).mean[0]
    except Exception:  # noqa: BLE001 - 统计失败用中灰保守叠
        mean = 128.0
    a = _overlay_alpha(mean)
    from PIL import Image
    black = Image.new("RGB", im.size, (0, 0, 0))
    return Image.blend(im.convert("RGB"), black, a)


def key_for(cover_url: str) -> str:
    raw = str(cover_url or "") + "|" + _BG_VERSION
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def file_path(key: str) -> str:
    return os.path.join(DIR, key + ".jpg")


def _blur_jpeg(data: bytes) -> bytes | None:
    try:
        from PIL import Image, ImageFilter, ImageStat

        im = Image.open(io.BytesIO(data)).convert("RGB")
        if im.width <= 0 or im.height <= 0:
            return None
        w = min(_BLUR_WIDTH, im.width)
        h = max(1, int(im.height * w / im.width))
        im = im.resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(radius=_BLUR_RADIUS))
        im = _darken(im, ImageStat)   # 叠自适应黑罩，保证白色歌词可读
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
