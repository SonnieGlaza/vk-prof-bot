FROM python:3.11-slim

# Ставим git и системные CA-сертификаты
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# Обновляем системный CA-bundle (это полезно для других утилит, но не обязательно для requests)
RUN update-ca-certificates

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "max_bot.py"]
