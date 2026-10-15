# =============================================================================
# 多阶段构建：
#   stage 1  node:22      编译 Vue 前端 -> frontend/dist
#   stage 2  python:3.11  安装后端依赖（独立层，业务代码改动不触发重装）
#   stage 3  runtime      最终镜像：FastAPI 单域名托管 API + 前端静态资源
# 构建产物即为可独立运行的完整应用：docker compose up --build 后
# 打开 http://127.0.0.1:8002/ 即登录页。
#
# 注：这里刻意**不写** `# syntax=docker/dockerfile:1.7`。该指令会强制拉取
# docker/dockerfile 前端镜像，而本项目 Dockerfile 只用了标准指令（无 heredoc /
# 新语法），用内置前端即可；省掉一次外部拉取 = 少一个构建失败点（国内镜像源
# 对 docker/ 命名空间经常 502，实测会直接卡住构建）。
# =============================================================================
ARG PYTHON_VERSION=3.11-slim
ARG NODE_VERSION=22-alpine

# ---------------- stage 1: 前端构建 ----------------
FROM node:${NODE_VERSION} AS frontend-build
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------------- stage 2: 后端依赖安装 ----------------
FROM python:${PYTHON_VERSION} AS backend-build
WORKDIR /app
# 国内网络友好：阿里云镜像（清华镜像部分网络不可用）+ 超时重试
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_RETRIES=5 \
    PIP_TIMEOUT=60
# 先用空包占位安装全部依赖：只有 pyproject.toml 变化才会触发这一层重跑，
# 平时改业务代码不会重新下载依赖（既快又避免网络超时）
COPY pyproject.toml README.md ./
RUN mkdir -p src/customer_support && touch src/customer_support/__init__.py \
    && pip install --no-cache-dir .
# 拷贝真实代码，仅重装包本身（--no-deps，秒级完成）
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# ---------------- stage 3: 运行时 ----------------
FROM python:${PYTHON_VERSION} AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # 关键：让应用代码走 /app/src（ROOT_DIR 据此解析为 /app），
    # 而不是 site-packages 里 pip 安装的副本（那会把 output/ 等路径指到 /usr/local）。
    PYTHONPATH=/app/src

COPY --from=backend-build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=backend-build /usr/local/bin /usr/local/bin
COPY src ./src
COPY data/knowledge_base ./data/knowledge_base
# 前端构建产物由后端单域名托管（app.py 检测 frontend/dist）
COPY --from=frontend-build /web/dist ./frontend/dist
# 容器内默认配置（真实配置经 env_file / 环境变量注入，优先级更高）
COPY .env.example .env

# 非 root 运行；output/logs 为挂载卷，需对其有写权限
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/output /app/logs \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8002

# 容器级健康检查：healthz 只探测进程存活，供 Docker 重启策略使用
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8002/healthz',timeout=4).status==200 else 1)"

CMD ["uvicorn", "customer_support.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8002"]
