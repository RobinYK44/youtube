#!/usr/bin/env bash
# Installs the Shorts bot on an Ubuntu server and keeps it running, also after a restart.
# Run again at any time to update. Usage (as root):
#   curl -fsSL https://raw.githubusercontent.com/RobinYK44/youtube/claude/youtube-shorts-streamers-b2gowb/server/install.sh | bash
set -euo pipefail
REPO=https://github.com/RobinYK44/youtube.git
BRANCH=claude/youtube-shorts-streamers-b2gowb
DIR=/opt/shortsbot

echo "==> Programma's installeren (ffmpeg, Python)..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3-venv ffmpeg fonts-dejavu-core >/dev/null

echo "==> Bot downloaden..."
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull -q --ff-only
else
  git clone -q --depth 1 -b "$BRANCH" "$REPO" "$DIR"
fi
mkdir -p "$DIR/data"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

cat > /etc/systemd/system/shortsbot.service <<SERVICE
[Unit]
Description=Shorts bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStartPre=/bin/sh -c 'if [ -f shortsbot.db ]; then mv shortsbot.db data/shortsbot.db; fi'
ExecStart=$DIR/venv/bin/python main.py
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable -q shortsbot

if [ -f "$DIR/.env" ] && [ -f "$DIR/youtube_token.json" ]; then
  systemctl restart shortsbot
  echo
  echo "✅ Klaar! De bot draait en start vanzelf opnieuw na een storing of herstart."
  echo "   Meekijken: journalctl -u shortsbot -f   (stoppen met kijken: Ctrl+C)"
else
  echo
  echo "✅ Geïnstalleerd. Zet nu je bestanden erop met naar_server.bat op je laptop."
fi
