"""LX06 通道映射表加载器（票 01 标定产物，数据非代码常量）。

映射表 JSON（`channel_map_lx06.json`）由 `deploy/lx06/kws-beamform-ab/
calibrate_channel_map.py` 对真机 200 段标定生成：ch0..ch5 = 从右下顶点起
逆时针每 60° 连续顶点（中位残差 0.281 样本、闭合差 0.306，quiet_near 50 段；
镜像等价类中取代表排列）。波束策略与未来任何多麦模块都应从这里取通道
几何，不要在代码里硬编码顶点顺序——旧代码的 ch0=正上假设已被真机证伪。

纯 stdlib：加载发生在 `ChannelDownmixer` 构造期，不引入 numpy 依赖。
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "lx06-channel-map/1"
N_CHANNELS = 6
DEFAULT_CHANNEL_MAP_PATH = Path(__file__).with_name("channel_map_lx06.json")


def load_channel_map(path: str | Path | None = None, data: dict | None = None) -> dict:
    """加载并校验通道映射表；返回带 `channel_coords_m`（6×(x, y) 元组）的字典。

    `data` 用于测试注入；缺省读包内默认资产。结构不对抛 ValueError。
    """
    if data is None:
        source = DEFAULT_CHANNEL_MAP_PATH if path is None else Path(path)
        data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"schema 不支持：{data.get('schema')!r}（期望 {SCHEMA!r}）")
    coords = data.get("channel_coords_m")
    if not isinstance(coords, list) or len(coords) != N_CHANNELS:
        raise ValueError(f"channel_coords_m 必须是 {N_CHANNELS} 个通道的坐标表")
    normalized = []
    for index, point in enumerate(coords):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"通道 {index} 坐标必须是 [x, y]")
        normalized.append((float(point[0]), float(point[1])))
    result = dict(data)
    result["channel_coords_m"] = normalized
    return result
