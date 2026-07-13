FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir pytest pytest-asyncio

COPY . .

EXPOSE 2999

CMD ["python", "main.py"]
