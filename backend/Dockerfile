FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY computer_use_demo /app/computer_use_demo
COPY pyproject.toml /app/pyproject.toml

EXPOSE 8000

CMD ["uvicorn", "computer_use_demo.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
