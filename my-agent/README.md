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
