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
WhatsApp session directory.

The bot sends a 07:00 Israel-time briefing, alerts the WhatsApp owner when it
restarts or encounters an agent failure, and keeps long-term assistant memory
in `.assistant_memory.json`.

## Deploying

**Deploys are automatic.** Every push to `main` that touches `my-agent/**`
triggers `.github/workflows/deploy-whatsapp-bot.yml`, which runs on a
self-hosted GitHub Actions runner living on the production server itself and
does the same pull/install/restart sequence `deploy.sh` always did — no SSH
required, since the runner only makes outbound connections to GitHub. This is
also why Claude Code on the web sessions can merge a fix and have it reach
production without ever needing server access: the sandbox's egress proxy
only carries HTTP/HTTPS on ports 80/443 (SSH is rejected outright, even
re-hosted on 443), so a direct `ssh root@138.199.159.146` from a web session
was never going to work — this workflow is the fix for that, not a
workaround of it.

Manual/fallback options still work if needed:

```sh
./deploy.sh
```

from a machine with real SSH access (e.g. Felix's Mac), or pulling directly
on the server (`cd /root/repo-update/my-agent && git pull && ...`).

### One-time runner setup (do this once, from a machine with SSH access)

1. GitHub repo → Settings → Actions → Runners → New self-hosted runner →
   Linux x64. Copy the `config.sh --url ... --token ...` command it gives you
   (the token is short-lived, generate a fresh one each time you do this).
2. On the server, as the same user that runs pm2 (`root`, matching the rest of
   this stack):
   ```sh
   mkdir -p /root/actions-runner && cd /root/actions-runner
   curl -o actions-runner.tar.gz -L <the URL from step 1>
   tar xzf actions-runner.tar.gz
   ./config.sh --url https://github.com/baigelbiz/felix-projects-for-fun \
     --token <TOKEN> --labels whatsapp-bot --unattended
   sudo ./svc.sh install
   sudo ./svc.sh start
   ```
   `svc.sh install` registers it as a systemd service so it survives reboots
   and restarts on crash — otherwise a server reboot silently kills the
   listener and deploys stop reaching production without any error, the same
   failure mode this whole change was meant to eliminate.
3. Confirm it shows "Idle" under Settings → Actions → Runners.

**Security note:** this repo is public. The workflow triggers only on `push`
to `main` (requires write access) and `workflow_dispatch` — never on
`pull_request`/`pull_request_target`. Do not add either of those triggers to
`deploy-whatsapp-bot.yml`; on a public repo with a self-hosted runner, that
would let anyone who opens a PR run arbitrary code on the production server.

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
