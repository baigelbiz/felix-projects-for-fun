# my-agent

A starter Claude Agent SDK (Python) project.

## Setup

```sh
python3.13 -m venv .venv          # already done
.venv/bin/pip install -r requirements.txt
```

## Auth

Uses your existing Claude Code login automatically. To bill a specific API key
instead, set `ANTHROPIC_API_KEY` (get one at https://console.anthropic.com/).

## Run

```sh
.venv/bin/python main.py
```

The WhatsApp bridge is `bot.js`; it uses `.env`, the local `.venv`, and the
WhatsApp session directory. For the production server, deploy the current
`main` branch with:

```sh
./deploy.sh
```

The bot sends a 07:00 Israel-time briefing, alerts the WhatsApp owner when it
restarts or encounters an agent failure, and keeps long-term assistant memory
in `.assistant_memory.json`.

## Deploying from a Claude Code on the web session

Claude Code on the web sessions **cannot SSH to the production server**: the
sandbox only permits outbound HTTP/HTTPS through an inspecting proxy (ports
80/443), and that proxy rejects the SSH protocol — including SSH re-hosted on
port 443 (it answers `HTTP/1.1 400 Bad Request`). Port 22 is blocked outright.
So a web session can't reach `root@138.199.159.146` directly, no matter how the
environment's network access is configured.

Deploy instead from a machine with real SSH access (e.g. Felix's Mac) by
running `./deploy.sh`, or by pulling on the server itself
(`cd /root/repo-update/my-agent && git pull && ...`).

## What's inside

- `main.py` — an agent with a custom `roll_dice` tool (in-process MCP server),
  auto-approved built-in tools (`Read`, `Glob`), streaming output with a tool-call
  trace, and a cost summary at the end.

## Next steps

- Change `system_prompt` and the `prompt` in `main.py`
- Add more tools with `@tool` and register them on the MCP server
- Add `Write`/`Edit`/`Bash` to `allowed_tools` to let the agent modify files
- Switch to `ClaudeSDKClient` for multi-turn conversations with memory
- Docs: https://code.claude.com/docs/en/agent-sdk/python
