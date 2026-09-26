FROM python:3.12-slim

# 时区固定为上海：定时任务/批次统计依赖本地时间，容器默认 UTC 会差 8 小时
ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && ln -sf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir pytest pytest-asyncio

COPY . .

EXPOSE 2999

CMD ["python", "main.py"]
