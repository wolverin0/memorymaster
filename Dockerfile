FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df AS builder

WORKDIR /app

# Copy project files
COPY pyproject.toml README.md ./
COPY memorymaster/ ./memorymaster/

# Install dependencies with extras
RUN pip install --no-cache-dir ".[mcp,qdrant,security]"


FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

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

# The image defaults to the HTTP dashboard. The stdio and authenticated HTTP
# MCP services remain explicit alternatives: memorymaster-mcp and
# memorymaster-mcp-http.
CMD ["memorymaster-dashboard", "--host", "0.0.0.0", "--port", "8765", "--db", "/data/memorymaster.db", "--workspace", "/data"]
