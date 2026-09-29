FROM python:3.11-slim

# Prevent .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Build tools for Python packages (bcrypt), plus runtime tools:
#   acl     – setfacl, per-user GPU device ACLs (services/resource_limits.py)
#   curl    – docker-compose healthcheck
#   procps  – ps/pgrep for process inspection
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    acl \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install JupyterLab (spawned by jupyter_manager service)
RUN pip install --no-cache-dir jupyterlab

# Create persistent directories:
#   /app/data     – SQLite database file
#   /jupyter_data – per-user notebook working directories + logs
RUN mkdir -p /app/data /jupyter_data

# Copy application source
COPY . .

# FastAPI application port
EXPOSE 8000

# Initialise database then launch the ASGI server.
# Single worker avoids SQLite write contention; use PostgreSQL + multiple
# workers for high-availability deployments.
CMD ["sh", "-c", "python init_db.py && uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1"]
