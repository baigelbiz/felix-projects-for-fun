#!/bin/bash
# Hydrates SSH access to the WhatsApp bot's production server (see
# my-agent/deploy.sh) from a secret, so Claude Code sessions can SSH in
# without the key ever living in the repo or the container image.
#
# Requires two things configured outside this script, on the environment
# itself (see my-agent/README.md "SSH access" section):
#   1. Network policy must allow egress to WHATSAPP_BOT_SSH_HOST on port 22.
#   2. An environment variable WHATSAPP_BOT_SSH_KEY holding the private key
#      (ed25519), base64-encoded onto a single line — Claude Code's cloud
#      environment variables are .env-format, one KEY=value per line, so a
#      raw multi-line PEM key can't be stored directly. The matching public
#      key must already be in root@<host>'s ~/.ssh/authorized_keys.
#      Generate the single-line value with: base64 -w0 id_ed25519
#
# Safe to run multiple times; a no-op if the secret isn't configured.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

if [ -z "${WHATSAPP_BOT_SSH_KEY:-}" ]; then
  exit 0
fi

HOST="${WHATSAPP_BOT_SSH_HOST:-138.199.159.146}"

if ! command -v ssh >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq openssh-client
fi

mkdir -p ~/.ssh
chmod 700 ~/.ssh

if ! base64 -d <<< "$WHATSAPP_BOT_SSH_KEY" > ~/.ssh/whatsapp_bot_deploy 2>/dev/null; then
  echo "WHATSAPP_BOT_SSH_KEY is set but isn't valid base64 — SSH access not configured." >&2
  exit 0
fi
chmod 600 ~/.ssh/whatsapp_bot_deploy

# accept-new pins the host key on first connect instead of prompting —
# there's no interactive terminal here to answer a yes/no prompt.
touch ~/.ssh/config
sed -i '/^Host whatsapp-bot$/,/^$/d' ~/.ssh/config
cat >> ~/.ssh/config << EOF
Host whatsapp-bot
  HostName $HOST
  User root
  IdentityFile ~/.ssh/whatsapp_bot_deploy
  StrictHostKeyChecking accept-new

EOF
chmod 600 ~/.ssh/config

echo "SSH access configured: run 'ssh whatsapp-bot' to reach the WhatsApp bot server."
