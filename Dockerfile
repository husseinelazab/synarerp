# صورة تشغيل سِينار للإنتاج
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    SYNAR_DEBUG=0 SYNAR_TIME_ZONE=Asia/Riyadh

WORKDIR /app
# مكتبات تشكيل النص العربي ورسم الصفحات لتوليد ملفات PDF (WeasyPrint)، وخط احتياطي للرموز
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# جمع الملفات الثابتة وقت البناء (بمفتاح مؤقت لا يُستخدم في التشغيل)
RUN SYNAR_SECRET_KEY=build-only-not-used python manage.py collectstatic --noinput -v 0 \
    && useradd --create-home --uid 1000 synar && mkdir -p /app/media && chown -R synar /app/media
USER synar

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=4).status == 200 else 1)"
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["web"]
