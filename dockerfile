FROM python:3.11-slim

# Ставим git и ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# Обновляем системный CA-bundle
RUN update-ca-certificates

RUN cat /app/full_certs.pem >> /etc/ssl/certs/ca-certificates.crt

# Переменные, которые заставляют requests использовать системные сертификаты
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# Копируем requirements и ставим зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код
COPY . .

CMD ["python", "max_bot.py"]
