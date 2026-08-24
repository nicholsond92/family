FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist the SQLite database on a mounted volume so deploys don't wipe data.
ENV HUB_DB=/data/hub.db
VOLUME /data

EXPOSE 8000
CMD ["python", "serve.py"]
