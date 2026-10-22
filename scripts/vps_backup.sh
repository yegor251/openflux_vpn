#!/bin/bash
# SRV-04: daily on-VPS backup of the bot state.
#
# Unencrypted, stored on the VPS itself - the admin's explicit choice
# (2026-10-23): guards against a broken registry / bad rotate / fat
# fingers, NOT against full VDS loss. Runs from oflux-backup.timer.
set -euo pipefail

BOT_DIR="/root/.config/oflux-bot"
BACKUP_DIR="/root/backups/oflux-bot"
KEEP=14
FILES="bot.yaml servers.yaml users.json bot_users.json ya-cookies.txt"

mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/oflux-bot-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -czf "$out" -C "$BOT_DIR" $FILES
chmod 600 "$out"

old="$(ls -1t "$BACKUP_DIR"/oflux-bot-*.tar.gz | tail -n +$((KEEP + 1)) || true)"
[ -n "$old" ] && rm -f $old

touch "$BOT_DIR/.last-backup"
chmod 600 "$BOT_DIR/.last-backup"
echo "backup ok: $out ($(du -h "$out" | cut -f1)), generations: $(ls -1 "$BACKUP_DIR"/oflux-bot-*.tar.gz | wc -l | tr -d ' ')"
