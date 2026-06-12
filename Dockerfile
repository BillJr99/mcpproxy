FROM python:3.12-slim

WORKDIR /app

# Node.js (LTS) is needed to run npx-based MCP providers.
# git is needed by repository providers (clone + build before spawn).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git \
    && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install uv so that uvx-based MCP package providers work out of the box.
# uv installs its binaries to /root/.local/bin; add that to PATH.
RUN pip install --no-cache-dir uv
ENV PATH="/root/.local/bin:$PATH"

COPY server.py config.py process_runner.py builtin_tools.py tool_registry.py rest_provider.py oauth_bootstrap.py ./
COPY frontend/ ./frontend/
COPY handlers/ ./handlers/

# tools/ is NOT baked in — supply at runtime via a volume mount.
# See docker-compose.yml and docker-compose.override.yml.

ENV MCP_TOOL_CONFIG_DIR=/app/tools
ENV MCP_ENV_FILE=/app/.env
ENV MCPPROXY_FILES_DIR=/app/files
ENV MCPPROXY_REPOS_DIR=/app/repos
ENV MCPPROXY_REST_AUTH_DIR=/app/.rest-auth

EXPOSE 8888 8889

CMD ["python", "server.py"]
