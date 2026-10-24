FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GEO_HOST=0.0.0.0 \
    GEO_PORT=8000

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY run.py config.example.json README.md LICENSE ./

# Optional dashboard password (empty = no auth)
ARG GEO_DASHBOARD_PASSWORD=""
ENV GEO_DASHBOARD_PASSWORD=${GEO_DASHBOARD_PASSWORD}

# Bind override: containers must bind 0.0.0.0 to be reachable; compose mounts a volume
# for the SQLite DB. Default local runs keep the 127.0.0.1-only binding.
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
