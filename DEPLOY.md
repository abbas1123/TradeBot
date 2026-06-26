# Running TradeBot 24/7 (with your PC off)

**Key fact:** when your PC is off, anything running on it stops too. To run 24/7 you need an
**always-on host** — a small cloud server (VPS) or a Raspberry Pi at home. The bot is
keyless paper trading (`--mode serve`), so this is safe and cheap.

## 1. Get an always-on host

| Option | Cost | Notes |
|---|---|---|
| **Oracle Cloud — Always Free** | Free | A free Arm VM that runs forever. Best free option. |
| **Hetzner Cloud** | ~€4/mo | Cheap, reliable (CX22). |
| **DigitalOcean / Contabo / Vultr** | ~$5/mo | Easy, popular. |
| **Raspberry Pi (home)** | one-off | Runs at home 24/7; needs your internet on. |

Pick **Ubuntu 22.04/24.04** when creating the server.

## 2. Install + copy the bot (on the server, via SSH)

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git tmux
# copy your project up (from your PC):  scp -r C:\Users\Abbbas\Desktop\TradeBot user@SERVER_IP:~/TradeBot
cd ~/TradeBot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env      # set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID (see telegram_setup.py)
```

## 3a. Simplest: run inside tmux (survives logout/SSH disconnect)

```bash
tmux new -s bot
. .venv/bin/activate
python main.py --mode serve --source live --top 15 --leverage 2 --risk 2 \
    --timeframe 1h --capital 1000 --no-open --report-min 30
# detach (leave it running): press Ctrl+B then D
# reattach later:  tmux attach -t bot
```
It keeps running after you close SSH / turn your PC off. Telegram sends 🔔 trade alerts and
a 📊 position report every `--report-min` minutes.

## 3b. Better: systemd service (auto-restarts on crash/reboot)

Create `/etc/systemd/system/tradebot.service`:
```ini
[Unit]
Description=TradeBot
After=network-online.target

[Service]
WorkingDirectory=/home/USER/TradeBot
ExecStart=/home/USER/TradeBot/.venv/bin/python main.py --mode serve --source live \
    --top 15 --leverage 2 --risk 2 --timeframe 1h --capital 1000 --no-open --report-min 30
Restart=always
RestartSec=10
User=USER

[Install]
WantedBy=multi-user.target
```
Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tradebot
sudo journalctl -u tradebot -f      # watch logs
```

## Notes
- `--no-open` skips the browser (headless server). The web dashboard still runs on
  `127.0.0.1:8000` (localhost only) — you monitor via **Telegram**, not the browser.
- This is **paper trading** (keyless, fake balance). For REAL orders you'd add Binance
  keys and `--mode paper`/`live` — but only after weeks of validation.
- Higher `--timeframe` (1h/4h/1d) = far fewer trades = far less fee drag. **Avoid 1m for
  anything but watching** — it bleeds fees.
