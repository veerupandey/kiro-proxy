# kiro-proxy

An OpenAI-compatible HTTP proxy that lets you use your Kiro Power subscription models
(Claude Sonnet/Opus) in any OpenAI-compatible client, including Kageha, Codex,
Continue.dev, and others.

## How it works

- Reads Builder-ID tokens directly from kiro-cli's local SQLite store
  (`~/.local/share/kiro-cli/data.sqlite3`) — no manual credential setup
- Auto-refreshes expired tokens via AWS OIDC and syncs them back so kiro-cli stays in sync
- Translates OpenAI chat completions (stream, non-stream, tool calling, multi-turn)
  to Kiro's CodeWhisperer event-stream API
- Pure stdlib — zero third-party dependencies

## Prerequisites

- Kiro IDE or kiro-cli installed and signed in (https://kiro.dev)
- Python 3.10+
- uv recommended (https://docs.astral.sh/uv/) or pip

## Install

    git clone https://github.com/veerupandey/kiro-proxy.git
    cd kiro-proxy

    # with uv (creates .venv automatically)
    uv sync

    # with pip
    pip install -e .

## Run

    kiro-proxy                 # default port 8787
    kiro-proxy --port 8807     # custom port
    python -m kiro_proxy.server --port 8787

Endpoints at http://127.0.0.1:<port>/v1:

  GET  /v1/models             — list available Kiro models
  POST /v1/chat/completions   — chat (stream, non-stream, tool calling, multi-turn)
  GET  /health                — health check

## Use with Kageha

Add to ~/.kageha/models.yaml:

    providers:
      kiro:
        base_url: http://127.0.0.1:8787/v1
        api_key: kiro-proxy

    models:
      - id: kiro/claude-opus-4.7
        provider: kiro
        name: claude-opus-4.7
        capabilities: [tool_calling]
        roles: [chat, agent, plan]

      - id: kiro/claude-sonnet-4.6
        provider: kiro
        name: claude-sonnet-4.6
        capabilities: [tool_calling]
        roles: [chat, agent, plan]

      - id: kiro/claude-opus-4.8
        provider: kiro
        name: claude-opus-4.8
        capabilities: [tool_calling]
        roles: [chat, agent, plan]

      - id: kiro/auto
        provider: kiro
        name: auto
        capabilities: [tool_calling]
        roles: [chat, agent, plan]

## Available models

The bundled catalog matches `kiro-cli chat --list-models --format json` from
the connected account on November 3, 2026:

```text
auto
claude-opus-5.5
claude-sonnet-5.5
claude-haiku-5.5
gpt-5.6-sol
gpt-5.6-terra
gpt-5.6-luna
kimi-k3
claude-opus-5
claude-sonnet-5
claude-opus-4.8
claude-opus-4.7
claude-opus-4.6
claude-sonnet-4.6
claude-opus-4.5
claude-sonnet-4.5
claude-haiku-4.5
deepseek-3.2
minimax-m2.5
minimax-m2.1
glm-5
qwen3-coder-next
```

This verifies catalog visibility, not successful inference for every model.
Availability and credit multipliers depend on your Kiro plan, profile, and
organization. New model IDs are forwarded unchanged so Kiro can validate access;
the proxy never silently substitutes another model. `/v1/models` reports tool
calling support for the bundled agent models.

## Auto-start on login

Add to ~/.zshrc or ~/.bashrc:

    if ! curl -sf http://127.0.0.1:8787/health >/dev/null 2>&1; then
      nohup kiro-proxy --port 8787 >> ~/.kiro-proxy.log 2>&1 &
    fi

## Development

    uv sync
    uv run pytest tests/ -v

## License

MIT
