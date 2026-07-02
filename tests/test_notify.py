"""Telegram sender: 4096-limit chunking + one retry, still fire-and-forget."""
from __future__ import annotations

from src.utils.notify import _chunks, send_telegram


class _OkResp:
    ok = True


def test_long_text_is_chunked_under_limit(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("requests.post", lambda url, data=None, timeout=None: calls.append(data) or _OkResp())
    assert send_telegram("tok", "chat", "x" * 9000) is True
    assert len(calls) == 3  # 4000 + 4000 + 1000
    assert all(len(d["text"]) <= 4000 for d in calls)


def test_chunks_split_on_newlines():
    text = ("line\n" * 1000).rstrip()  # 4999 chars
    parts = _chunks(text, limit=4000)
    assert len(parts) == 2
    assert all(not p.startswith("\n") and len(p) <= 4000 for p in parts)
    assert "\n".join(parts).count("line") == 1000  # nothing lost


def test_failed_post_retried_once(monkeypatch):
    n = {"calls": 0}

    def post(url, data=None, timeout=None):
        n["calls"] += 1
        if n["calls"] == 1:
            raise ConnectionError("telegram down")
        return _OkResp()

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert send_telegram("tok", "chat", "salam") is True
    assert n["calls"] == 2  # one failure, one retry, then success


def test_missing_token_is_silent_noop(monkeypatch):
    def _must_not_be_called(*a, **k):
        raise AssertionError("requests.post must not be called without a token")

    monkeypatch.setattr("requests.post", _must_not_be_called)
    assert send_telegram("", "chat", "hi") is False
