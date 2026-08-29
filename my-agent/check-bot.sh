#!/bin/bash
# WhatsApp bot watchdog — run from cron every 5 minutes.
# Restarts the bot if pm2 reports it down, and drops an alert in the outbox so
# the bot itself tells Felix what happened once it's back up (the bot's own
# in-process alerts can't fire while the process is dead).
#
# Cron: */5 * * * * /bin/bash /root/my-agent/check-bot.sh

# Cron runs with a minimal PATH that often lacks wherever pm2/node live
# (nvm installs, /usr/local/bin, ...). Without this, pm2/python3 silently
# fail to resolve, STATUS comes back empty, and the block below thinks the
# bot is down forever — restarting nothing but writing a new alert file
# every 5 minutes indefinitely.
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
if ! command -v pm2 >/dev/null 2>&1 && [ -s "$HOME/.nvm/nvm.sh" ]; then
    # shellcheck disable=SC1090
    . "$HOME/.nvm/nvm.sh"
fi

AGENT_DIR="/root/my-agent"
LOG="/root/check-bot.log"
ALERT_COOLDOWN_SEC=1800  # don't pile up a new alert file more than every 30m
TS=$(date "+%Y-%m-%d %H:%M:%S")

if ! command -v pm2 >/dev/null 2>&1; then
    echo "[$TS] ALERT — pm2 not found on PATH ($PATH), cannot check/restart the bot" >> "$LOG"
    exit 1
fi

STATUS=$(pm2 jlist 2>/dev/null | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    if p.get('name') == 'whatsapp-agent':
        print(p['pm2_env']['status']); break
" 2>/dev/null)

if [ "$STATUS" != "online" ]; then
    echo "[$TS] ALERT — status='$STATUS', restarting..." >> "$LOG"
    pm2 restart whatsapp-agent >> "$LOG" 2>&1

    mkdir -p "$AGENT_DIR/.outbox"
    NOW=$(date +%s)
    LAST_ALERT_TS=0
    LAST_ALERT_FILE=$(ls -t "$AGENT_DIR"/.outbox/alert_*.txt 2>/dev/null | head -1)
    if [ -n "$LAST_ALERT_FILE" ]; then
        LAST_ALERT_TS=$(basename "$LAST_ALERT_FILE" .txt | sed 's/^alert_//')
    fi
    if [ $(( NOW - ${LAST_ALERT_TS:-0} )) -ge "$ALERT_COOLDOWN_SEC" ]; then
        echo "⚠️ Watchdog: the bot was down (status: ${STATUS:-unknown}) and was restarted at $TS." \
            > "$AGENT_DIR/.outbox/alert_${NOW}.txt"
    fi
fi
