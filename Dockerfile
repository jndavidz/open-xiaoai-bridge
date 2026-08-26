FROM python:3.12-slim AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# 国内加速：apt 源 / uv(pypi) / rustup / cargo crates
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
ENV UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV RUSTUP_DIST_SERVER=https://rsproxy.cn RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
RUN mkdir -p /root/.cargo && printf '[source.crates-io]\nreplace-with = "rsproxy-sparse"\n[source.rsproxy-sparse]\nregistry = "sparse+https://rsproxy.cn/index/"\n' > /root/.cargo/config.toml

# 更新源
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential pkg-config patchelf cmake libportaudio2 portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

# 安装 uv
COPY --from=ghcr.nju.edu.cn/astral-sh/uv:0.7 /uv /uvx /bin/

# 设置环境变量
ENV BASH_ENV=/root/.bash_env
RUN touch "$BASH_ENV"
RUN echo '. "$BASH_ENV"' >> "$HOME/.bashrc"
RUN echo '[ -s "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"' >> "$BASH_ENV"

# 设置工作目录
WORKDIR /app

# 复制项目文件
COPY . .

# 安装锁定依赖（保持 pyproject.toml 与 uv.lock 一致）
RUN uv sync --locked --no-install-project --no-editable

# 构建 Rust 扩展并安装
RUN uv run maturin build --release --manifest-path native/Cargo.toml && uv remove maturin


FROM python:3.12-slim

WORKDIR /app

# 更新源
RUN apt-get update && apt-get install -y --no-install-recommends \
    libzstd1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY main.py .
COPY config.py .
COPY core ./core
COPY scripts ./scripts


# Ensure sherpa_onnx can locate onnxruntime shared library at runtime.
RUN set -eux; \
    ort_dir="/app/.venv/lib/python3.12/site-packages/onnxruntime/capi"; \
    if [ ! -f "$ort_dir/libonnxruntime.so" ]; then \
      ln -s "$(basename "$(ls "$ort_dir"/libonnxruntime.so.* | head -n 1)")" "$ort_dir/libonnxruntime.so"; \
    fi

ENV LD_LIBRARY_PATH=/app/.venv/lib/python3.12/site-packages/onnxruntime/capi

# 暴露 API 服务器端口
EXPOSE 9092

# 先初始化关键词模型（小智或外部后端启用时），然后启动主程序
# 兼容 OPENCLAW_ENABLE (新) 和 OPENCLAW_ENABLED (旧)
CMD ["/bin/bash", "-c", "source /app/.venv/bin/activate && OPENCLAW_VAL=\"${OPENCLAW_ENABLE:-${OPENCLAW_ENABLED:-}}\"; if [[ \"${XIAOZHI_ENABLE:-}\" =~ ^(1|true|yes)$ ]] || [[ \"$OPENCLAW_VAL\" =~ ^(1|true|yes)$ ]] || [[ \"${OPENAI_ENABLE:-}\" =~ ^(1|true|yes)$ ]] || [[ \"${QWENPAW_ENABLE:-}\" =~ ^(1|true|yes)$ ]]; then python core/services/audio/kws/keywords.py; fi && python main.py"]
