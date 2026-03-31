# ══════════════════════════════════════════════
# services/ingestion/Dockerfile
# ══════════════════════════════════════════════
FROM python:3.11-slim AS ingestion

WORKDIR /app

# System deps for tree-sitter build + git
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
