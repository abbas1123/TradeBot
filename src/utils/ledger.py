"""Append-only balance ledger: one JSON line per position open/close (equity snapshot).

Lets you see the exact balance at the instant every position was opened and closed —
a running record of how the account evolved, separate from the trade list.
"""
from __future__ import annotations

import json
from pathlib import Path


def append_ledger(path: str | None, record: dict) -> None:
    """Append one JSON record as a line to the ledger file. No-op if path is falsy."""
    if not path:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # ledger is observability — never let it break trading
