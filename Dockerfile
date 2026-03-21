FROM python:3.12-slim AS builder

WORKDIR /app

# Copy project files
COPY pyproject.toml README.md ./
COPY memorymaster/ ./memorymaster/

# Install dependencies with extras
RUN pip install --no-cache-dir ".[mcp,qdrant,security]"


FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# Copy binaries from builder
COPY --from=builder /usr/local/bin/memorymaster* /usr/local/bin/

# Copy source code
COPY memorymaster/ ./memorymaster/
COPY pyproject.toml README.md ./

# Environment variables
ENV MEMORYMASTER_DEFAULT_DB=/data/memorymaster.db
ENV MEMORYMASTER_WORKSPACE=/data

# Volume for data persistence
VOLUME /data

# Expose dashboard port
EXPOSE 8765

# Default command: run MCP server
CMD ["memorymaster-mcp"]
