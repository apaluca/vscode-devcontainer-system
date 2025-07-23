FROM python:3.11-slim

WORKDIR /app

# Install Node.js and required dependencies for devcontainer CLI
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    git \
    build-essential \
    ca-certificates \
    gnupg \
    lsb-release \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 (LTS)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs

# Install devcontainer CLI
RUN npm install -g @devcontainers/cli

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ .

# Create directory for devcontainer builds
RUN mkdir -p /tmp/devcontainer-builds

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV DOCKER_HOST=tcp://localhost:2375

# Expose the API port
EXPOSE 8080

# Start the FastAPI application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]