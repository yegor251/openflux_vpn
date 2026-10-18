#!/bin/bash
# Deploy/refresh the telegram bot on the VPS (root@159.194.227.248).
# Idempotent: safe to re-run after every git push. Fills nothing secret:
# bot.yaml (token + alert_tg_id) is created once, by hand.
set -euo pipefail
HOST=root@159.194.227.248
REPO=/root/openflux_vpn

ssh -o BatchMode=yes "$HOST" "cd $REPO && git pull -q origin main"
ssh -o BatchMode=yes "$HOST" "cd $REPO &&
    [ -d .venv ] || python3 -m venv .venv &&
    .venv/bin/pip install -q -e . &&
    mkdir -p /root/.config/oflux-bot &&
    [ -f /root/.config/oflux-bot/bot.yaml ] || cat > /root/.config/oflux-bot/bot.yaml <<'EOF'
bot_token: \"FILL_ME_FROM_BOTFATHER\"
alert_tg_id: 0
max_channels_per_user: 1
EOF
    chmod 600 /root/.config/oflux-bot/bot.yaml"
ssh -o BatchMode=yes "$HOST" "cp $REPO/deploy/oflux-bot.service /etc/systemd/system/ &&
    systemctl daemon-reload && systemctl enable oflux-bot >/dev/null 2>&1"
echo "deployed. if bot.yaml is still FILL_ME, fill it on the VPS, then:"
echo "  ssh $HOST 'systemctl restart oflux-bot && journalctl -u oflux-bot -f'"
