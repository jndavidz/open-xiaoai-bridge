# syntax 前缀故意不写：避免构建时从 docker.io 拉 frontend（网络不稳）；
# 本机 BuildKit 内置 frontend 已支持 RUN --mount cache（docker 23+）。
# reproducibility（ticket 14）：
#   - 基础镜像按 manifest digest 钉死（升级 = 手工换 digest）；
#   - --platform 固定 linux/amd64；
#   - rustup 钉 toolchain 版本 + minimal profile；
#   - Cargo.lock 进构建上下文（.dockerignore 已移除），git 依赖钉 commit；
#   - cargo/uv 缓存走 BuildKit cache mount（不进层、可复用）；
#   - Rust 扩展由 maturin 构建后端经 `uv sync` 安装（隔离构建环境），
#     不再用 `uv run maturin build` + `uv remove`（后者层内改写锁文件且重复编译）。
FROM --platform=linux/amd64 python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# 国内加速：apt 源 / uv(pypi) / rustup / cargo crates
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
ENV UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV RUSTUP_DIST_SERVER=https://rsproxy.cn RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
# 钉版（ticket 14）：升级需手工改此值；生成时实测 stable = 1.99.1（2026-10-13）
ENV RUST_TOOLCHAIN=1.99.1
RUN mkdir -p /root/.cargo && printf '[source.crates-io]\nreplace-with = "rsproxy-sparse"\n[source.rsproxy-sparse]\nregistry = "sparse+https://rsproxy.cn/index/"\n' > /root/.cargo/config.toml

# 更新源
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential pkg-config patchelf cmake libportaudio2 portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 Rust（钉 toolchain + minimal，不再随 stable 隐式漂移）
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain "$RUST_TOOLCHAIN" --profile minimal

# 安装 uv（digest 钉版）
COPY --from=ghcr.nju.edu.cn/astral-sh/uv:0.7@sha256:629240833dd25d03949509fc01ceff56ae74f5e5f0fd264da634dd2f70e9cc70 /uv /uvx /bin/

# 设置环境变量
ENV BASH_ENV=/root/.bash_env
RUN touch "$BASH_ENV"
RUN echo '. "$BASH_ENV"' >> "$HOME/.bashrc"
RUN echo '[ -s "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"' >> "$BASH_ENV"

# 设置工作目录
WORKDIR /app

# 层缓存优化：按变更频率从低到高拷贝——native/（含 Cargo.lock）先拷，
# core/ 等高频源码最后拷，使 Rust 编译仅在 native/ 或依赖变化时重跑；
# cargo/uv 缓存经 cache mount 跨构建复用，一改不再全量重编
# ① 依赖清单（uv.lock 锁定全部版本与 git 依赖 commit）
COPY pyproject.toml uv.lock ./

# ② Rust 源（maturin 后端编译所需；Cargo.lock 随目录进入）
COPY native/ ./native/

# ③ 安装锁定依赖 + 本项目（maturin 隔离构建环境编译 Rust 扩展并装入 .venv）
RUN --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/root/.cargo/git \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/app/native/target \
    uv sync --locked --no-editable

# ④ 高频变更源码（core/ 改动只失效本层）
COPY core ./core
COPY main.py .
COPY scripts ./scripts


FROM --platform=linux/amd64 python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

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
