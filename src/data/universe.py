"""Dynamic trading universe: pick the most liquid quote pairs from the exchange.

Lets the bot scan the whole market instead of a hardcoded list. Liquidity (24h quote
volume) is the single most important filter at any account size — illiquid coins have
wide spreads and bad fills that destroy a strategy regardless of its edge.
"""
from __future__ import annotations

# quote-side assets we never want as a *base* (stable/wrapped — no real trend to trade)
_STABLE_BASES = {
    "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "USDD", "EUR", "GBP", "AEUR",
    "USDT", "PAXG", "WBTC", "WBETH", "USD1", "USDE", "GUSD", "EURI", "XUSD",
    "PYUSD", "RLUSD", "USDG", "USDS", "EURC", "EURT", "EURQ", "EURR", "USDQ",
}
_LEV_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")
# real coins that would otherwise match a leveraged suffix (SYRUP ends with UP, etc.)
_NOT_LEVERAGED = {"SYRUP", "JUP", "SUPER"}


def _looks_leveraged(base: str) -> bool:
    # leveraged tokens are <coin><suffix> (BTCUP, ETHBULL, XRP3L) — require a coin-like
    # prefix of >= 2 chars so JUP ("J"+"UP") and plain names never false-positive
    if base in _NOT_LEVERAGED:
        return False
    return any(base.endswith(sfx) and len(base) - len(sfx) >= 2 for sfx in _LEV_SUFFIXES)


def top_symbols(exchange, n: int = 20, quote: str = "USDT", exclude_stables: bool = True) -> list[str]:
    """Return the top-`n` `*/quote` spot symbols by 24h quote volume."""
    exchange.load_markets()
    try:
        tickers = exchange.fetch_tickers()
    except Exception:
        # fallback: just use markets order if tickers unavailable
        return [s for s in exchange.symbols if s.endswith(f"/{quote}")][:n]

    rows: list[tuple[str, float]] = []
    for sym, t in tickers.items():
        if not sym.endswith(f"/{quote}"):
            continue
        m = exchange.markets.get(sym, {})
        if not m.get("spot", True) or not m.get("active", True):
            continue
        base = sym.split("/")[0]
        if _looks_leveraged(base):
            continue
        if exclude_stables and base in _STABLE_BASES:
            continue
        qv = t.get("quoteVolume")
        if qv is None:
            # derive from baseVolume * last if needed
            bv, last = t.get("baseVolume"), t.get("last")
            qv = (bv * last) if (bv and last) else 0.0
        rows.append((sym, float(qv or 0.0)))

    rows.sort(key=lambda r: r[1], reverse=True)
    return [s for s, _ in rows[:n]]
