# Model Arena MCP server for Amazon Bedrock AgentCore Runtime (linux/arm64).
# Build from the repository root:  docker build -f infra/mcp/Dockerfile .
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
# only the server's dependencies, pinned to the versions in uv.lock
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-hashes --no-emit-project -o /tmp/lock.txt \
 && uv pip install --system --no-cache --constraint /tmp/lock.txt mcp numpy pandas pyarrow boto3 \
 && rm /tmp/lock.txt
COPY arena ./arena
ENV ARENA_MODE=remote ARENA_OUTPUTS=/tmp/arena ARENA_S3_CLIENT=boto3 PYTHONUNBUFFERED=1
RUN useradd -m arena && mkdir -p /tmp/arena && chown arena /tmp/arena
USER arena
EXPOSE 8000
CMD ["python", "-m", "arena.mcp_server"]
