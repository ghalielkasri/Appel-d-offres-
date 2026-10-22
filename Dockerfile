FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Africa/Casablanca

WORKDIR /app

# lxml a besoin de ces bibliotheques ; curl sert au controle de sante.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY pyproject.toml ./
RUN pip install --no-deps -e . && mkdir -p /app/data

# Le conteneur tourne sans privileges.
RUN useradd --create-home --uid 10001 radar && chown -R radar:radar /app
USER radar

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
