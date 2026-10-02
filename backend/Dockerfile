FROM python:3.11-slim

WORKDIR /app

# 安装 psycopg2/asyncpg 编译依赖及基础工具
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖（使用阿里云 PyPI 镜像加速）
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 拷贝后端核心代码
COPY backend/ /app/backend/

# 创建解析产物本地工作区
RUN mkdir -p /app/data/parsed

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
