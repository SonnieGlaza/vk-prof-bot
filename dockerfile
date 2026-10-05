FROM python:3.11-slim

# Ставим git (чтобы можно было ставить git-зависимости, если позже понадобятся)
# и ca-certificates (системные сертификаты)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# Обновляем системный CA-bundle
RUN update-ca-certificates
RUN cat /app/full_certs.pem >> /etc/ssl/certs/ca-certificates.crt

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

# Явно говорим requests использовать certifi (это самый стабильный вариант)
# Если certifi установлен, он содержит свежие сертификаты и работает везде.
ENV REQUESTS_CA_BUNDLE=/usr/local/lib/python3.11/site-packages/certifi/cacert.pem
ENV SSL_CERT_FILE=/usr/local/lib/python3.11/site-packages/certifi/cacert.pem

CMD ["python", "max_bot.py"]
