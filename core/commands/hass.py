"""Home Assistant REST client（指令层）。

凭据走环境变量：HA_BASE_URL / HA_TOKEN（与 core/tools/hass.py 的只读查询一致）。
失败只记日志不打断语音流程——指令动作是「尽力而为」语义，
单个 HA 调用失败不应中断后续播报/收尾步骤。
"""

import os

import aiohttp

from core.utils.logger import logger


async def hass_action(domain: str, service: str, data: dict | None = None,
                      *, playback_domain: str = ""):
    """调用 HA 服务（异步非阻塞）。失败只记日志不打断语音流程。

    `playback_domain` 仅进日志（hifi/lx06/cch），让 HA 调用可按播放域追踪；
    不改变请求语义（P4/05b）。
    """
    tag = f" domain={playback_domain}" if playback_domain else ""
    base_url = os.environ.get("HA_BASE_URL", "http://10.10.10.2:8123").rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}/api/services/{domain}/{service}",
                headers={
                    "Authorization": f"Bearer {os.environ.get('HA_TOKEN', '')}",
                    "Content-Type": "application/json",
                },
                json=data or {},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    logger.error(f"[commands] HA {domain}.{service} -> HTTP {resp.status}{tag}")
                else:
                    logger.info(f"[commands] HA {domain}.{service} OK{tag}")
                return resp.status
    except Exception as e:
        logger.error(f"[commands] HA {domain}.{service} failed: {e}{tag}")
        return None
