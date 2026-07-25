FROM python:3.11-slim

# Install OS dependencies for quickfix
RUN apt-get update && \
    apt-get install -y build-essential git curl && \
    rm -rf /var/lib/apt/lists/*

# Create working directory
WORKDIR /opt/mockx

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY ./src ./src
COPY ./templates ./templates

# Create necessary directories
RUN mkdir -p logs store

# Create non-root user
RUN useradd -ms /bin/bash trader && \
    chown -R trader:trader /opt/mockx

# Switch to non-root user
USER trader

# Default config path via environment variable
ENV CFG_PATH=/etc/mockx/config.yml
# Make application code discoverable
ENV PYTHONPATH=/opt/mockx/src

# Entry point
ENTRYPOINT ["python", "-m", "mockx.cli"]
