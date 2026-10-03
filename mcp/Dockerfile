# Mailu MCP server image (stdio). Build from the repo root:
#   docker build -f mcp/Dockerfile -t <you>/mailu-mcp:latest mcp/
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="mailu-mcp" \
      org.opencontainers.image.description="Mailu MCP server: mail + admin + Gmail import over stdio" \
      org.opencontainers.image.source="https://github.com/Mailu/Mailu"

WORKDIR /app
COPY mailu_mcp/ /app/mailu_mcp/
RUN python3 -m compileall -q /app/mailu_mcp \
    && rm -rf /app/mailu_mcp/__pycache__ \
    && useradd --create-home --uid 10001 mcp
USER 10001
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1
EXPOSE 8000

# Default: MCP over stdio. For HTTP behind a load balancer:
#   docker run -e MCP_HTTP_TOKEN=... -p 8000:8000 <img> --http --host 0.0.0.0
ENTRYPOINT ["python3", "-m", "mailu_mcp.server"]
