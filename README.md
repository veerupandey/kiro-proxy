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

  auto              — Kiro picks the model
  claude-sonnet-5.5 — Experimental; requires Kiro account/organization access
  claude-opus-5.5   — Experimental; requires Kiro account/organization access
  claude-opus-4.8
  claude-opus-4.7
  claude-opus-4.6
  claude-opus-4.5
  claude-sonnet-4.6
  claude-sonnet-4.5
  claude-haiku-4.5

Auto and the 4.x choices passed live requests on the connected account on October
16, 2026. The two 5.5 choices are exposed explicitly, but live requests on that
account returned an organization access error. Adding a catalog entry cannot
enable an entitlement at Kiro.
Availability depends on the account and organization. New model names are sent
upstream unchanged; unavailable or misspelled names return the upstream error
instead of silently using Sonnet 4.6. The public Kiro catalog can include models
that your account cannot access.

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
