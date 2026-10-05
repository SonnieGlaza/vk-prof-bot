FROM python:3.11-slim

# 1. Ставим git и ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# 2. Обновляем системный CA-bundle
RUN update-ca-certificates

# 3. Если у тебя есть кастомный full_certs.pem — склеиваем его с системным
# (если файла нет — эта строка не сломает сборку)
RUN cat /app/full_certs.pem /etc/ssl/certs/ca-certificates.crt > /tmp/combined_certs.pem 2>/dev/null || true
RUN cp /tmp/combined_certs.pem /etc/ssl/certs/ca-certificates.crt 2>/dev/null || true

# 4. Явно указываем Python использовать правильный CA-bundle
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# 5. Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 6. Копирование кода
COPY . .

CMD ["python", "max_bot.py"]
