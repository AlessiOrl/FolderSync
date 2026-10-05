FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY sync.py settings.py app.py nfo.py poster.py config.json /app/
COPY web /app/web

EXPOSE 8080
CMD ["python", "/app/app.py"]
