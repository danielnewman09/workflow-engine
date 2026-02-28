# workflow-engine

SQLite-backed multi-agent workflow orchestration engine.

A project-agnostic engine for concurrent multi-agent ticket orchestration.
The engine knows about tickets, phases, agents, and gates — but nothing about
any specific project. All project-specific configuration lives in the consuming
repository's `.workflow/` directory.

Extracted from [MSD-CPP](https://github.com/danielnewman09/MSD-CPP) (ticket 0083a).

## Installation

```bash
pip install git+https://github.com/danielnewman09/workflow-engine.git
```

## Quick Start

### MCP Server (stdio mode — Claude Code)

```bash
python -m workflow_engine.server build/Debug/docs/workflow.db --project-root .
```

### MCP Server (SSE mode — Docker)

```bash
docker compose up
```

Then register in your `.mcp.json`:

```json
{
  "mcpServers": {
    "workflow": {
      "url": "http://localhost:8080/sse"
    }
  }
}
```

### Human CLI

```bash
# List pending human review gates
workflow-engine gates

# Approve a gate
workflow-engine approve <gate-id>

# Show ticket status
workflow-engine status 0083

# List all tickets
workflow-engine list

# Import tickets from markdown
workflow-engine import-tickets

# Show blocked phases and gates
workflow-engine blocked
```

## Consuming Repo Layout

Your repository needs a `.workflow/` directory:

```
your-repo/
├── .workflow/
│   ├── phases.yaml    # Phase definitions, agent type mappings, conditions
│   └── config.yaml   # Database path, timeouts, agent registry
├── tickets/           # Ticket markdown files
└── ...
```

See the [MSD-CPP phases.yaml](https://github.com/danielnewman09/MSD-CPP/blob/main/.workflow/phases.yaml)
for a complete example.

## MCP Registration (stdio)

```json
{
  "mcpServers": {
    "workflow": {
      "command": "python",
      "args": [
        "-m", "workflow_engine.server",
        "build/Debug/docs/workflow.db",
        "--project-root", "."
      ]
    }
  }
}
```

## Architecture

- `workflow_engine/engine/` — Core: schema, models, scheduler, atomic claim, state machine
- `workflow_engine/server/` — FastMCP server exposing 20+ MCP tools
- `workflow_engine/cli/` — Human CLI for gate management and queue inspection

The SQLite database uses WAL mode with `BEGIN IMMEDIATE` transactions for safe
concurrent multi-agent access (validated: 40/40 concurrent claim trials with 0 duplicates).

## Development

```bash
git clone https://github.com/danielnewman09/workflow-engine.git
cd workflow-engine
pip install -e ".[dev]"
pytest tests/
```

## License

MIT
