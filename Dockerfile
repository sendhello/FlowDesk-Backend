FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Install runtime dependencies first (better layer caching).
COPY pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install \
        "fastapi>=0.115" "uvicorn[standard]>=0.30" "sqlalchemy[asyncio]>=2.0.30" \
        "asyncpg>=0.29" "alembic>=1.13" "pydantic>=2.7" "pydantic-settings>=2.3" \
        "pyjwt[crypto]>=2.8" "httpx>=0.27" "email-validator>=2.1"

COPY . .

EXPOSE 8080

# Fly runs `alembic upgrade head` as the release_command (see fly.toml) before this starts.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
