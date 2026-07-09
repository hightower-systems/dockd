FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libhidapi-dev curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:5001/health || exit 1

# Apply any pending schema migrations before the app starts, then launch.
# Alembic reads DATABASE_URL from the environment (set as a container
# secret), matching how the app connects.
CMD ["sh", "-c", "alembic upgrade head && python run.py"]
