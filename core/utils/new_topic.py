"""「换个话题」暗号识别纯函数（spec topic-switch-and-memory 票 02）。

双通道（spec 方案 B，用户拍板）：
- 精确通道：文本去标点去空白后与 exact 词表整句相等；
- 模糊通道：去标点后长度 ≤8 字，且与 fuzzy 词表某词编辑距离 ≤2。

安全门槛：**长句（>8 字）永不进模糊通道**——「我想给你讲个换话题的笑话」
这类正常表达不会被误拦截（spec user story 3）；8 字门槛是防误拦核心，
调整词表时勿放宽。
"""

import re

# 模糊通道长度上限（去标点后的字符数）
FUZZY_MAX_LEN = 8
# 编辑距离容忍度
FUZZY_MAX_DISTANCE = 2

_PUNCT_RE = re.compile(r"[\s，。！？、；：\"'“”‘’（）()\[\]【】,.\!?;:~～…—·]+")


def strip_punctuation(text: str) -> str:
    """去标点与空白（ASR 输出常带标点/空格噪声）。"""
    return _PUNCT_RE.sub("", text or "")


def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离（O(mn) DP；词表规模个位数，无需优化）。"""
    if a == b:
        return 0
    if not a or not b:
        return len(a) + len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def match_new_topic(text: str, *, exact: list[str], fuzzy: list[str]) -> bool:
    """ASR 文本 → 是否触发「换个话题」。

    exact/fuzzy 任一通道命中即 True；两通道均空词表恒 False。
    """
    normalized = strip_punctuation(text)
    if not normalized:
        return False

    for word in exact or []:
        if strip_punctuation(word) and normalized == strip_punctuation(word):
            return True

    if len(normalized) > FUZZY_MAX_LEN:
        return False
    for word in fuzzy or []:
        cleaned = strip_punctuation(word)
        if not cleaned:
            continue
        if edit_distance(normalized, cleaned) <= FUZZY_MAX_DISTANCE:
            return True
    return False
