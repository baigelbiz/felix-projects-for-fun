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

## WhatsApp transport: migrating from whatsapp-web.js to Baileys

`bot.js` uses **whatsapp-web.js** (Puppeteer/Chrome). As of mid-2026 its
`downloadMedia()` is broken against WhatsApp's current `2.3xxx` web client
(upstream issues wwebjs/whatsapp-web.js#201828 / #201833 — still open), which
broke voice-note transcription and image handling. The old workaround (pinning
to an older `2.2xxx` web snapshot) no longer works — those snapshots were
removed from the version repo.

`bot.baileys.js` is a drop-in replacement built on **Baileys** (native
WebSocket, no browser), which has a first-class media-download API. It preserves
every behavior of `bot.js` (allowed-sender filtering, `@m`/`@r`/`@s` commands,
voice/image/location handling, morning briefing, outbox, state persistence,
proactive alerts). The Python backend (`agent_reply.py`) is unchanged.

Auth is stored separately in `.baileys_auth/` (bot.js uses `.wwebjs_auth/`), so
the two never collide and the old bot stays instantly restorable.

### Test the Baileys bot before cutting over

Run it in the **foreground** on the server, alongside the still-running pm2 bot,
and scan the QR with the bot's second number:

```sh
cd /root/my-agent
node bot.baileys.js          # prints a QR — scan with the bot's second number
```

Then, from Felix's phone, message the bot: a text (`@a hi`), a **voice note**,
and an **image**. Confirm all three work (the voice note transcribes and the
image is read — the whole point of the migration). `Ctrl-C` to stop.

### Cut pm2 over to Baileys (after it's verified)

```sh
cd /root/my-agent
pm2 stop whatsapp-agent
pm2 start bot.baileys.js --name whatsapp-agent --update-env
pm2 save
```

To roll back, `pm2 start bot.js` the same way. Once Baileys is trusted, `bot.js`
and the `whatsapp-web.js` dependency can be removed.

### Transcribing recorded phone calls

Beyond quick voice notes, the Baileys bot handles **recorded phone calls** sent
as audio (a voice note, an inline audio clip, or a file/document whose type is
audio — m4a, mp3, ogg, wav, etc.). It downloads and transcribes them with
Whisper (auto-detecting the language for recordings), then acts on the
transcript:

- **Send a caption with the recording** and the bot does exactly that with it —
  e.g. caption "pull out the action items and who owns each" or "summarize in 5
  bullets."
- **No caption** → it defaults to a concise WhatsApp-friendly bullet summary
  (key points, decisions, names, numbers/dates, action items).

A ptt voice note with no caption keeps the old quick conversational reply.

**ffmpeg (recommended).** If `ffmpeg` is on the server, every recording is first
transcoded to compact 16 kHz mono Opus — this normalizes any format (including
ones Whisper rejects outright, like `.amr`/`.3gp`) and shrinks it to roughly
7 MB per hour of speech, so long calls stay under Whisper's 25 MB limit. Install
once with `apt-get install -y ffmpeg`. Without ffmpeg the bot still works, but
audio goes to Whisper as-is: exotic formats may fail and a recording over 25 MB
gets a clear "too big — send a shorter/compressed clip" reply instead of a
silent failure.

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
