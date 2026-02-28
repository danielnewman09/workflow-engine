FROM python:3.12-slim

WORKDIR /app

# Install the package
COPY pyproject.toml README.md ./
COPY workflow_engine/ ./workflow_engine/
RUN pip install --no-cache-dir .

# Default: start MCP server in SSE mode
# The consuming repo mounts its project root at /project
# and provides a .workflow/ config directory there.
ENV PROJECT_ROOT=/project
ENV DB_PATH=/data/workflow.db
ENV PORT=8080

# Expose SSE port
EXPOSE 8080

# Create data directory for the database
RUN mkdir -p /data /project

ENTRYPOINT ["sh", "-c", \
    "python -m workflow_engine.server ${DB_PATH} --project-root ${PROJECT_ROOT} --transport sse --port ${PORT}"]
