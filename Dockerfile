# OWID Data Tools — container image
# Used by Cloudflare Containers (wrangler deploy builds this) or any Docker host.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY owid_tools.py mcp_server.py ./

ENV HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# Health check (also used as Cloudflare Containers pingEndpoint)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "mcp_server.py"]
