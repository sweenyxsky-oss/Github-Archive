FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN groupadd -g 568 appuser && useradd -u 568 -g 568 -r -s /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R 568:568 /app /data

USER 568:568
EXPOSE 8080

CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]
