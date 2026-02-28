FROM python:3.12-slim

WORKDIR /app

# Install git and GitHub CLI (needed for branch/commit/PR operations)
RUN apt-get update && \
    apt-get install -y --no-install-recommends git curl ca-certificates && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends gh && \
    apt-get purge -y curl && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Install the package with traceability extras (tree-sitter)
COPY pyproject.toml README.md ./
COPY workflow_engine/ ./workflow_engine/
RUN pip install --no-cache-dir ".[traceability]"

# Default: start MCP server in streamable-http mode
# The consuming repo mounts its project root at /project
# and provides a .workflow/ config directory there.
ENV PROJECT_ROOT=/project
ENV DB_PATH=/data/workflow.db
ENV PORT=8080
ENV TRANSPORT=streamable-http

EXPOSE 8080

# Create data directory for the database
RUN mkdir -p /data /project

# Configure gh as git credential helper and mark /project as safe
ENTRYPOINT ["sh", "-c", \
    "gh auth setup-git && git config --global --add safe.directory /project && \
     python -m workflow_engine.server ${DB_PATH} --project-root ${PROJECT_ROOT} --transport ${TRANSPORT} --port ${PORT}"]
