# ─────────────────────────────────────────────────────────
#  Virtual Edge Backend – Dockerfile
#  Build:  docker build -t virtualedge-backend .
#  Run:    docker run -p 8000:8000 --env-file .env virtualedge-backend
# ─────────────────────────────────────────────────────────

FROM python:3.11-slim

# System deps (needed by some cryptography/bcrypt wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libssl-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Generate VAPID keys if they don't exist
RUN python generate_vapid.py || true

# Non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port (override with PORT env var via Procfile / platform)
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Production server — no --reload in container
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--log-level", "info"]
