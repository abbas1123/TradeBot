"""Optional Telegram notifications (uses `requests`, which ships with ccxt — no new dep).

Configure TELEGRAM_TOKEN + TELEGRAM_CHAT_ID in .env. If unset, Notifier is a silent no-op,
so the bot runs fine without it.
"""
from __future__ import annotations


_TG_CHUNK = 4000  # stay under Telegram's 4096-char hard limit per message


def _chunks(text: str, limit: int = _TG_CHUNK) -> list[str]:
    """Split on the last newline inside each window so lines stay whole."""
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > 0 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        return False
    try:
        import time

        import requests
    except Exception:
        return False
    ok = True
    for chunk in _chunks(text):
        sent = False
        for attempt in range(2):  # one retry per chunk, then give up (fire-and-forget)
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": chat_id, "text": chunk},
                    timeout=8,
                )
                if bool(getattr(r, "ok", False)):
                    sent = True
                    break
            except Exception:
                pass  # treat as a failed attempt; retry below
            if attempt == 0:
                time.sleep(1.0)
        ok = ok and sent
    return ok


class Notifier:
    def __init__(self, settings):
        self.token = getattr(settings, "telegram_token", "") or ""
        self.chat_id = getattr(settings, "telegram_chat_id", "") or ""
        self.enabled = bool(self.token and self.chat_id)

    def notify(self, text: str) -> None:
        if self.enabled:
            send_telegram(self.token, self.chat_id, text)

    def report(self, equity: float, cash: float, rows: list, extra: str = "") -> None:
        """Send a position/equity snapshot. rows: list of (symbol, side, entry, mark, unreal)."""
        if self.enabled:
            send_telegram(self.token, self.chat_id, format_report(equity, cash, rows, extra))


def format_report(equity: float, cash: float, rows: list, extra: str = "") -> str:
    """Bilingual (AZ/EN) position + equity snapshot for Telegram."""
    lines = [f"📊 Balans / Equity: {equity:,.2f} USDT" + (f"  ({extra})" if extra else "")]
    lines.append(f"💵 Boş nağd / Free cash: {cash:,.2f}")
    if rows:
        lines.append("📈 Açıq mövqelər / Open positions:")
        for sym, side, entry, mark, unreal in rows:
            mark_ = "🟢" if unreal >= 0 else "🔴"
            lines.append(f"{mark_} {sym} {side}  {entry:,.4g}→{mark:,.4g}  PnL {unreal:+.2f}")
    else:
        lines.append("😴 Mövqe yoxdur / No open positions (flat)")
    return "\n".join(lines)
