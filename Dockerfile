FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PGID=100 \
    FILE_MODE=0764 \
    DIRECTORY_MODE=0764

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY permissions.py sync.py settings.py app.py nfo.py metadata.py posters.py config.json /app/
COPY defaults /app/defaults
COPY web /app/web

EXPOSE 8080
CMD ["python", "/app/app.py"]
