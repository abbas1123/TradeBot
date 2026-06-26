#!/usr/bin/env bash
# One-command server setup for TradeBot (run on an Ubuntu VM after copying the project up).
set -e
cd "$(dirname "$0")"

echo "==> Installing system packages ..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git tmux

echo "==> Creating venv + installing dependencies ..."
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

[ -f .env ] || cp .env.example .env

echo ""
echo "==> Done. Next steps:"
echo "  1) nano .env        # set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID"
echo "  2) python telegram_setup.py   # verify Telegram"
echo "  3) Start it 24/7 inside tmux:"
echo "       tmux new -s bot"
echo "       . .venv/bin/activate"
echo "       python main.py --mode serve --source live --top 15 --leverage 2 --risk 2 \\"
echo "           --timeframe 1h --capital 1000 --no-open --report-min 30"
echo "     detach with Ctrl+B then D ; reattach with: tmux attach -t bot"
echo "  (or use the systemd service in DEPLOY.md for auto-restart)"
