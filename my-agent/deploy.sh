#!/usr/bin/env bash
set -euo pipefail

SERVER="${SERVER:-root@138.199.159.146}"
REMOTE_APP="${REMOTE_APP:-/root/my-agent}"
REMOTE_REPO="${REMOTE_REPO:-/root/repo-update}"

git diff --check
git push origin main

ssh "$SERVER" "
  set -euo pipefail
  cd '$REMOTE_REPO'
  git pull --ff-only origin main
  cp agent_reply.py bot.js deploy.sh '$REMOTE_APP'/
  chmod +x '$REMOTE_APP'/deploy.sh
  cd '$REMOTE_APP'
  .venv/bin/python -m py_compile agent_reply.py
  node --check bot.js
  .venv/bin/python smoke_test.py
  pm2 restart whatsapp-agent --update-env
  pm2 describe whatsapp-agent | sed -n '1,35p'
"

echo "Deployed to $SERVER:$REMOTE_APP"
