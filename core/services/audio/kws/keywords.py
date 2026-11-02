import os
from pathlib import Path


def init_project_context():
    """动态导入父模块"""
    import os
    import sys

    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../../..")
    )
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


init_project_context()

from core.utils.config import ConfigManager
from core.utils.file import get_model_file_path
from core.utils.logger import logger


def should_generate_keywords():
    """Return whether keyword generation should run."""
    import os

    xiaozhi_enabled = os.environ.get("XIAOZHI_ENABLE", "").lower() in ("1", "true", "yes")
    # 兼容 OPENCLAW_ENABLE (新) 和 OPENCLAW_ENABLED (旧)
    openclaw_env = os.environ.get("OPENCLAW_ENABLE") or os.environ.get("OPENCLAW_ENABLED") or ""
    openclaw_enabled = openclaw_env.lower() in ("1", "true", "yes")
    openai_enabled = os.environ.get("OPENAI_ENABLE", "").lower() in (
        "1",
        "true",
        "yes",
    )
    qwenpaw_enabled = os.environ.get("QWENPAW_ENABLE", "").lower() in (
        "1",
        "true",
        "yes",
    )

    if (
        not xiaozhi_enabled
        and not openclaw_enabled
        and not openai_enabled
        and not qwenpaw_enabled
    ):
        return False, "XIAOZHI_ENABLE, OPENCLAW_ENABLE/OPENCLAW_ENABLED, OPENAI_ENABLE and QWENPAW_ENABLE are all disabled"

    return True, ""


def get_args():
    config = ConfigManager.instance()
    tokens = get_model_file_path("tokens.txt")
    bpe_model = get_model_file_path("bpe.model")
    # tokens_type 随模型形态自适应（2026-11-01 主路切候选 wenetspeech 拼音 tokens
    # 模型后，硬编码 cjkchar+bpe + bpe.model 必需导致启动崩溃循环）：
    #   - 有 bpe.model（现网模型）：cjkchar+bpe；
    #   - 无 bpe.model（拼音 tokens 模型，官方不发布 bpe）：ppinyin，bpe 不再必需。
    has_bpe = Path(bpe_model).is_file()
    tokens_type = "cjkchar+bpe" if has_bpe else "ppinyin"
    output = get_model_file_path("keywords.txt")
    keywords = config.get_app_config("wakeup.keywords", [])
    gates = get_word_gates()
    texts = [f"{keyword.upper()}" for keyword in keywords]
    return locals()


# ---- 逐词门槛（music-far spec 票 03）----
# 编译/解析/自检纯函数在 core/utils/word_gates.py（keywords 与 shadow 共用，
# 避免 shadow 独立加载时触发 kws 包 heavyweight 初始化）；
# 这里只留依赖 ConfigManager 的配置读取。
from core.utils.word_gates import (  # noqa: E402,F401  (re-export 供旧引用)
    compile_keyword_line,
    parse_keyword_line,
    verify_keyword_lines,
)


def get_word_gates() -> dict:
    """读取并校验逐词门槛配置；非法条目告警后丢弃（不影响其余词）。"""
    config = ConfigManager.instance()
    raw = config.get_app_config("kws.keyword_gates", {}) or {}
    if not isinstance(raw, dict):
        logger.warning("[KWS] kws.keyword_gates 必须是 {词: {...}}，已忽略", module="KWS")
        return {}
    gates: dict = {}
    for word, spec in raw.items():
        if not isinstance(word, str) or not word or not isinstance(spec, dict):
            logger.warning(f"[KWS] keyword_gates 非法条目 {word!r}，已忽略", module="KWS")
            continue
        gate: dict = {}
        for key in ("boost", "threshold"):
            value = spec.get(key)
            if value is None:
                continue
            try:
                gate[key] = float(value)
            except (TypeError, ValueError):
                logger.warning(
                    f"[KWS] keyword_gates[{word!r}].{key} 非法值 {value!r}，已忽略",
                    module="KWS",
                )
        if gate:
            gates[word] = gate
    return gates


def missing_required_files():
    """启动自检：tokens.txt 必需；bpe.model 仅在 cjkchar+bpe 形态下必需。"""
    tokens = get_model_file_path("tokens.txt")
    bpe_model = get_model_file_path("bpe.model")
    required = [tokens] if Path(bpe_model).is_file() else []
    required.insert(0, tokens)
    return [path for path in required if not Path(path).is_file()]


def main():
    should_run, reason = should_generate_keywords()
    if not should_run:
        logger.debug(f"Keyword generation skipped: {reason}", module="KWS")
        return 0

    missing_files = missing_required_files()
    if missing_files:
        logger.debug(
            "Keyword generation failed: missing model files: "
            f"{', '.join(missing_files)}",
            module="KWS",
        )
        return 1

    from sherpa_onnx import text2token

    args = get_args()
    encoded_texts = text2token(
        args["texts"],
        tokens=args["tokens"],
        tokens_type=args["tokens_type"],
        **({"bpe_model": args["bpe_model"]} if args["has_bpe"] else {}),
    )
    with open(args["output"], "w", encoding="utf8") as f:
        # 显示名写原词（与 shadow build_keywords 一致）：@串进入唤醒文本参与
        # 免唤醒指令路由匹配，拼音串会让中文词表匹配全部落空。
        # 逐词门槛（票 03）：配置了 boost/threshold 的词带 :x / #x 后缀。
        for word, txt in zip(args["keywords"], encoded_texts):
            f.write(
                compile_keyword_line(txt, word, args["gates"].get(word)) + "\n"
            )
    # 词表自检（票 03）：任一行 token 编译失败/显示名丢失 → 启动期告警
    problems = verify_keyword_lines(args["output"], args["keywords"])
    for problem in problems:
        logger.warning(f"[KWS] 词表自检告警：{problem}", module="KWS")
    logger.debug(f"Keyword file generated: {args['output']}", module="KWS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
