"""Connect Telegram in one step: finds your chat id and sends a test message.

HOW TO:
  1) In Telegram, open @BotFather -> send /newbot -> pick a name -> copy the TOKEN it gives.
  2) Put it in .env:   TELEGRAM_TOKEN=123456:ABC...   (leave TELEGRAM_CHAT_ID blank for now)
  3) Open YOUR new bot in Telegram and send it any message, e.g. "hi".
  4) Run:   python telegram_setup.py
       - it prints the chat id(s) that messaged the bot -> copy TELEGRAM_CHAT_ID=... into .env
       - re-run it once TELEGRAM_CHAT_ID is set; it sends a test message to confirm.
"""
from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

import requests

token = (os.getenv("TELEGRAM_TOKEN") or "").strip()
chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

if not token:
    print("No TELEGRAM_TOKEN found in .env.")
    print("Get one from @BotFather (/newbot), then add to .env:  TELEGRAM_TOKEN=...")
    raise SystemExit(1)

print("Checking your bot for recent messages ...")
try:
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
except Exception as e:
    print(f"Could not reach Telegram: {e}")
    raise SystemExit(1)

if not r.get("ok"):
    print(f"Telegram rejected the token: {r}")
    print("Double-check TELEGRAM_TOKEN in .env.")
    raise SystemExit(1)

found = {}
for u in r.get("result", []):
    msg = u.get("message") or u.get("channel_post") or u.get("edited_message") or {}
    ch = msg.get("chat") or {}
    if ch.get("id") is not None:
        found[ch["id"]] = ch.get("username") or ch.get("title") or ch.get("first_name") or "?"

if found:
    print("\nChat IDs that have messaged your bot:")
    for cid, name in found.items():
        print(f"   TELEGRAM_CHAT_ID={cid}   ({name})")
    print("Copy the right one into .env.")
else:
    print("\nNo messages seen yet. Open your bot in Telegram, send it 'hi', then run this again.")

if chat:
    print(f"\nSending a test message to chat {chat} ...")
    tr = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": "✅ TradeBot Telegram connected!"},
        timeout=15,
    ).json()
    print("Test message sent ✅" if tr.get("ok") else f"Failed: {tr}")
else:
    print("\nTELEGRAM_CHAT_ID is not set yet. Add it to .env (from the list above) and re-run.")
