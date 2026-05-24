FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# config/ and handlers/ are NOT baked in — they are supplied at runtime
# via volume mounts (bind mounts in dev, named volumes in prod).
# See docker-compose.yml and docker-compose.override.yml.

ENV MCP_TOOL_CONFIG_DIR=/app/config/tools
EXPOSE 8888

CMD ["python", "server.py"]
