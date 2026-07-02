"""Market data access via CCXT: paginated OHLCV, balances, symbol filters.

Public OHLCV needs no API keys, so backtests run with a keyless exchange. Historical
candles are cached to parquet (append-merge) so repeated backtests make zero network
calls. The still-forming last candle is always dropped to avoid acting on an
incomplete bar.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

OHLCV_COLS = ["timestamp", "open", "high", "low", "close", "volume"]
# earliest sensible BTC history floor (ms) when no `since` is given
_DEFAULT_SINCE_MS = 1_502_928_000_000  # 2017-08-17


def build_public_exchange(exchange_name: str = "binance", testnet: bool = False):
    """A keyless CCXT exchange for public data (OHLCV/markets)."""
    import ccxt

    klass = getattr(ccxt, exchange_name)
    ex = klass({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    if testnet:
        ex.set_sandbox_mode(True)
    return ex


def _now_ms() -> int:
    return int(time.time() * 1000)


class Fetcher:
    def __init__(self, exchange, cache_dir: str | None = "data_store/ohlcv"):
        self.exchange = exchange
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # --- public OHLCV ----------------------------------------------------
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1d",
        since: int | None = None,
        until: int | None = None,
        limit_per_call: int = 1000,
        use_cache: bool = True,
        drop_unclosed: bool = True,
    ) -> pd.DataFrame:
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        if since is None:
            since = _DEFAULT_SINCE_MS
        if until is None:
            until = _now_ms()

        cached = self._cache_load(symbol, timeframe) if use_cache else None

        # decide what we still need to fetch (only the missing tail, by default)
        fetch_from = since
        if cached is not None and not cached.empty:
            last_cached = int(cached["timestamp"].iloc[-1].timestamp() * 1000)
            if last_cached >= until - tf_ms:
                return self._finalize(cached, since, until, tf_ms, drop_unclosed, symbol)
            fetch_from = max(since, last_cached + tf_ms)

        rows: list[list] = []
        cursor = fetch_from
        while cursor < until:
            batch = self._with_retry(
                self.exchange.fetch_ohlcv, symbol, timeframe, since=cursor, limit=limit_per_call
            )
            if not batch:
                break
            rows.extend(batch)
            last_ts = batch[-1][0]
            next_cursor = last_ts + tf_ms
            if next_cursor <= cursor:  # no progress guard
                break
            cursor = next_cursor
            if len(batch) < limit_per_call:
                break

        fresh = self._to_dataframe(rows)
        merged = self._merge(cached, fresh)
        if use_cache and not merged.empty:
            # never persist a still-forming bar, or it gets frozen as a stale "closed" candle
            self._cache_save(symbol, timeframe, self._drop_forming(merged, tf_ms))
        return self._finalize(merged, since, until, tf_ms, drop_unclosed, symbol)

    def fetch_latest(self, symbol: str, timeframe: str, lookback_bars: int,
                     drop_unclosed: bool = True) -> pd.DataFrame:
        """Just enough recent CLOSED bars to compute indicators.

        drop_unclosed=False keeps the still-forming bar too — its close is the freshest
        price, used for real-time stop checks between bar closes (never for signals)."""
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        since = _now_ms() - (lookback_bars + 2) * tf_ms
        return self.fetch_ohlcv(symbol, timeframe, since=since, use_cache=False,
                                drop_unclosed=drop_unclosed)

    # --- account ---------------------------------------------------------
    def fetch_balance(self) -> dict:
        return self._with_retry(self.exchange.fetch_balance)

    def free_quote(self, quote: str = "USDT") -> float:
        bal = self.fetch_balance()
        try:
            return float(bal.get(quote, {}).get("free", 0.0) or 0.0)
        except (AttributeError, TypeError):
            return 0.0

    def fetch_open_orders(self, symbol: str | None = None) -> list:
        return self._with_retry(self.exchange.fetch_open_orders, symbol)

    def load_markets(self, reload: bool = False) -> dict:
        return self._with_retry(self.exchange.load_markets, reload)

    # --- helpers ---------------------------------------------------------
    def _with_retry(self, fn, *args, max_attempts: int = 5, base: float = 1.0, **kwargs):
        import ccxt

        for attempt in range(max_attempts):
            try:
                return fn(*args, **kwargs)
            except (ccxt.DDoSProtection,) as e:  # 418 -> back off hard
                sleep = max(base * 2**attempt, 5.0)
            except (ccxt.RateLimitExceeded,) as e:  # 429
                sleep = base * 2**attempt
            except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as e:
                if attempt == max_attempts - 1:
                    raise
                sleep = base * 2**attempt
            except (ccxt.AuthenticationError, ccxt.BadSymbol):
                raise  # retrying won't help
            time.sleep(min(sleep, 60.0))
        raise RuntimeError(f"retries exhausted calling {getattr(fn, '__name__', fn)}")

    @staticmethod
    def _to_dataframe(rows: list[list]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=OHLCV_COLS)
        df = pd.DataFrame(rows, columns=OHLCV_COLS)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
        return df.reset_index(drop=True)

    @staticmethod
    def _merge(cached: pd.DataFrame | None, fresh: pd.DataFrame) -> pd.DataFrame:
        if cached is None or cached.empty:
            return fresh.reset_index(drop=True)
        if fresh is None or fresh.empty:
            return cached.reset_index(drop=True)
        out = pd.concat([cached, fresh], ignore_index=True)
        out = out.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
        return out.reset_index(drop=True)

    @staticmethod
    def _drop_forming(df, tf_ms) -> pd.DataFrame:
        """Drop the last row if its bar has not closed yet (now < bar_open + tf)."""
        if df is None or df.empty:
            return df
        last_open_ms = int(df["timestamp"].iloc[-1].timestamp() * 1000)
        if _now_ms() < last_open_ms + tf_ms:
            return df.iloc[:-1]
        return df

    @staticmethod
    def _validate(df: pd.DataFrame, tf_ms: int, symbol: str) -> pd.DataFrame:
        """Drop provably-malformed bars; warn (only) on gaps — never fabricate candles."""
        if df is None or df.empty:
            return df
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        good = (
            (h >= np.maximum(o, c)) & (l <= np.minimum(o, c))
            & (o > 0) & (h > 0) & (l > 0) & (c > 0)
        )
        n_bad = int((~good).sum())
        if n_bad:
            logger.warning(f"{symbol}: dropped {n_bad} malformed OHLC bar(s)")
            df = df[good].reset_index(drop=True)
        gaps = int((df["timestamp"].diff().dt.total_seconds().mul(1000.0) > tf_ms * 1.5).sum())
        if gaps:
            logger.warning(f"{symbol}: {gaps} gap(s) (missing bars) in the series")
        return df

    @staticmethod
    def _finalize(df, since, until, tf_ms, drop_unclosed, symbol: str = "") -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(columns=OHLCV_COLS)
        out = df.copy()
        lo = pd.to_datetime(since, unit="ms", utc=True)
        hi = pd.to_datetime(until, unit="ms", utc=True)
        out = out[(out["timestamp"] >= lo) & (out["timestamp"] <= hi)]
        out = Fetcher._validate(out.reset_index(drop=True), tf_ms, symbol)
        if drop_unclosed:
            out = Fetcher._drop_forming(out, tf_ms)
        return out.reset_index(drop=True)

    def _cache_path(self, symbol: str, timeframe: str) -> Path | None:
        """Cache key includes the exchange id — mixing exchanges must not share files.

        NB: files written before this fix were always named BINANCE_*; if you ever
        fetched with a non-binance EXCHANGE back then, that cache is ambiguous —
        delete data_store/ohlcv or run once with --refresh-data."""
        if not self.cache_dir:
            return None
        safe = symbol.replace("/", "-")
        ex_id = str(getattr(self.exchange, "id", "exchange") or "exchange").upper()
        return self.cache_dir / f"{ex_id}_{safe}_{timeframe}.parquet"

    def _cache_load(self, symbol: str, timeframe: str) -> pd.DataFrame | None:
        path = self._cache_path(symbol, timeframe)
        if not path or not path.exists():
            return None
        try:
            return pd.read_parquet(path)
        except Exception:
            return None

    def _cache_save(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        path = self._cache_path(symbol, timeframe)
        if not path:
            return
        try:
            df.to_parquet(path, index=False)
        except Exception:
            df.to_csv(path.with_suffix(".csv"), index=False)
