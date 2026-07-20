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

## SSH access to the production server (for Claude Code sessions)

`.claude/hooks/session-start.sh` can hydrate SSH access to the deploy target
(`root@138.199.159.146`, see `deploy.sh`) at the start of every Claude Code
session, so sessions can check on / restart / debug the live bot directly.
It's a no-op unless both of these are set up on the environment — neither can
be done from inside a session, both are environment-level config on
claude.ai/code (cloud icon → hover the environment → settings icon):

1. **Network access**: set to **Custom** and add `138.199.159.146` to
   **Allowed domains**. By default sessions only get **Trusted** access
   (package registries, GitHub, etc.) — this host isn't on that list.
   Note the proxy is HTTP/HTTPS-based; whether it actually tunnels raw SSH
   (port 22) to a custom entry isn't guaranteed by the docs and needs to be
   verified once it's set.
2. **Environment variable `WHATSAPP_BOT_SSH_KEY`** — there's no dedicated
   secrets store; environment variables are stored as plain `.env`-format
   text (one `KEY=value` per line) in the environment config, visible to
   anyone who can edit that environment. Because a private key is normally
   multi-line, store it **base64-encoded onto a single line**:
   `base64 -w0 id_ed25519`. The hook decodes it back into a real key file.
   The matching public key must already be in `root@138.199.159.146`'s
   `~/.ssh/authorized_keys`.

Once both are set, a session can run `ssh whatsapp-bot` directly. Consider
scoping the key server-side (e.g. a non-root deploy user, or a
`command=`-restricted `authorized_keys` entry) rather than granting full root
— an autonomous session with standing production SSH access is a real
increase in blast radius.

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
