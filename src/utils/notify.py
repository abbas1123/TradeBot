"""Optional Telegram notifications (uses `requests`, which ships with ccxt — no new dep).

Configure TELEGRAM_TOKEN + TELEGRAM_CHAT_ID in .env. If unset, Notifier is a silent no-op,
so the bot runs fine without it.
"""
from __future__ import annotations


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        return False
    try:
        import requests

        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=8,
        )
        return bool(getattr(r, "ok", False))
    except Exception:
        return False


class Notifier:
    def __init__(self, settings):
        self.token = getattr(settings, "telegram_token", "") or ""
        self.chat_id = getattr(settings, "telegram_chat_id", "") or ""
        self.enabled = bool(self.token and self.chat_id)

    def notify(self, text: str) -> None:
        if self.enabled:
            send_telegram(self.token, self.chat_id, text)
