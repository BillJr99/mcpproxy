FROM python:3.12-slim

WORKDIR /app

# git is needed by repo_loader to clone/pull external provider repos
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py config.py repo_loader.py ./
COPY frontend/ ./frontend/
COPY handlers/ ./handlers/

# tools/ and repos/ are NOT baked in — supply at runtime via volumes.
# See docker-compose.yml and docker-compose.override.yml.

ENV MCP_TOOL_CONFIG_DIR=/app/tools
ENV MCP_REPOS_DIR=/app/repos
ENV MCP_ENV_FILE=/app/.env

EXPOSE 8888 8889

CMD ["python", "server.py"]
