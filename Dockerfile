FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data /backups /app/static/vendor \
    && python scripts/fetch_vendor.py --output /app/static/vendor/quagga.min.js

EXPOSE 3055
CMD ["gunicorn", "--bind", "0.0.0.0:3055", "--workers", "2", "--threads", "4", "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
