"""Fetcher data-quality guards: cache keying, OHLC integrity, gaps — no network."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
from loguru import logger

from src.data.fetcher import Fetcher


def test_cache_key_includes_exchange(tmp_path):
    fk = Fetcher(SimpleNamespace(id="kraken"), cache_dir=str(tmp_path))
    fb = Fetcher(SimpleNamespace(id="binance"), cache_dir=str(tmp_path))
    pk, pb = fk._cache_path("BTC/USDT", "1h"), fb._cache_path("BTC/USDT", "1h")
    assert pk != pb  # kraken data must never share a file with binance data
    assert pb.name == "BINANCE_BTC-USDT_1h.parquet"  # legacy binance filenames unchanged
    assert pk.name.startswith("KRAKEN_")


def test_validate_drops_malformed_bars(make_ohlcv):
    # bar 1: high (99) < close (101) — a provably-broken candle from a feed glitch
    df = make_ohlcv([100, 101, 102], highs=[101, 99, 103], lows=[99, 98, 101])
    out = Fetcher._validate(df, 86_400_000, "T/USDT")
    assert len(out) == 2
    assert 101.0 not in out["close"].values


def test_validate_warns_on_gaps_but_keeps_rows(make_ohlcv):
    df = make_ohlcv([1, 2, 3])
    df.loc[2, "timestamp"] = df.loc[1, "timestamp"] + pd.Timedelta(days=5)  # 4 missing bars
    msgs: list[str] = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        out = Fetcher._validate(df, 86_400_000, "T/USDT")
    finally:
        logger.remove(sink)
    assert len(out) == 3  # gaps only warn — bars are never fabricated or dropped
    assert any("gap" in m for m in msgs)


def test_merge_dedupes_and_sorts(make_ohlcv):
    a = make_ohlcv([1, 2], start="2020-01-01")
    b = make_ohlcv([20, 3], start="2020-01-02")  # first row overlaps a's second bar
    out = Fetcher._merge(a, b)
    assert len(out) == 3
    assert out["timestamp"].is_monotonic_increasing
    dup_ts = pd.Timestamp("2020-01-02", tz="UTC")
    assert out.loc[out["timestamp"] == dup_ts, "close"].iloc[0] == 20.0  # keep="last": fresh wins


def test_drop_forming_removes_open_bar():
    now = pd.Timestamp.now(tz="UTC")
    df = pd.DataFrame({
        "timestamp": [now - pd.Timedelta(days=2), now - pd.Timedelta(seconds=10)],
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [1.0, 2.0], "volume": [1.0, 1.0],
    })
    out = Fetcher._drop_forming(df, 86_400_000)  # 1d bars: the 10s-old bar is still forming
    assert len(out) == 1
