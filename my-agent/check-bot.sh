#!/bin/bash
# WhatsApp bot watchdog — run from cron every 5 minutes.
# Restarts the bot if pm2 reports it down, and drops an alert in the outbox so
# the bot itself tells Felix what happened once it's back up (the bot's own
# in-process alerts can't fire while the process is dead).
#
# Cron: */5 * * * * /bin/bash /root/my-agent/check-bot.sh

AGENT_DIR="/root/my-agent"
LOG="/root/check-bot.log"
TS=$(date "+%Y-%m-%d %H:%M:%S")

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
    echo "⚠️ Watchdog: the bot was down (status: ${STATUS:-unknown}) and was restarted at $TS." \
        > "$AGENT_DIR/.outbox/alert_$(date +%s).txt"
fi
