"""工具：查询实时天气（Open-Meteo，无需 API key，只读）。

经纬度可省略（取 HOME_LAT / HOME_LON 环境变量，缺省为北京），
模型也可在调用时显式传入其它城市。返回精简的中文播报串，适合语音朗读。
"""

import os

import aiohttp

from core.utils.logger import logger

NAME = "weather_get"

# WMO weather code -> 中文（精简，适合 TTS）
_WMO = {
    0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴小冰雹", 99: "雷阵雨伴大冰雹",
}

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "查询指定经纬度的实时天气（温度、天气、风速）。无需 key。"
            "用于回答『今天天气怎么样』『出门要带伞吗』。默认查家里坐标，"
            "可传 lat/lon 查其它地点。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "纬度，默认家坐标"},
                "lon": {"type": "number", "description": "经度，默认家坐标"},
            },
            "required": [],
        },
    },
}


def _home_coords() -> tuple[float, float]:
    try:
        lat = float(os.environ.get("HOME_LAT", "39.9042"))
        lon = float(os.environ.get("HOME_LON", "116.4074"))
    except (TypeError, ValueError):
        lat, lon = 39.9042, 116.4074
    return lat, lon


async def run(args: dict) -> str:
    lat = float(args.get("lat")) if args.get("lat") is not None else _home_coords()[0]
    lon = float(args.get("lon")) if args.get("lon") is not None else _home_coords()[1]
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,weather_code,wind_speed_10m&timezone=auto"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status >= 400:
                    return f"天气服务错误 {resp.status}"
                data = await resp.json()
                cur = (data.get("current") or {})
                code = cur.get("weather_code", 0)
                temp = cur.get("temperature_2m")
                wind = cur.get("wind_speed_10m")
                desc = _WMO.get(code, f"天气代码{code}")
                return f"当前{desc}，气温{temp}℃，风速{wind}公里/小时"
    except Exception as exc:
        logger.error(f"[tools/weather] {type(exc).__name__}: {exc}")
        return f"天气查询失败：{exc}"
