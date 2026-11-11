# ---------- 构建阶段：安装依赖到 venv ----------
# 国内网络走腾讯云 PyPI 镜像；与运行阶段同基座，venv 可直接拷贝
FROM python:3.13-slim AS build

ENV PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.tencent.com \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 先拷贝依赖清单，充分利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- 运行阶段 ----------
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && useradd -u 10001 -r -s /usr/sbin/nologin appuser \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY mysqlagent ./mysqlagent
COPY main.py config.yaml.example ./

USER appuser
EXPOSE 8080
ENTRYPOINT ["python", "main.py"]
# 默认读取挂载的配置文件；纯环境变量部署时（见 docker-compose.yml）
# 容器内不存在该文件会自动回退到环境变量 + 默认值
CMD ["-config", "/etc/mysqlagent/config.yaml"]
