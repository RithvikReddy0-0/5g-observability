FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir flask requests prometheus_client

CMD ["python", "server.py"]
