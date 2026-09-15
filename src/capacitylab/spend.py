"""Persistent model-spend ledger so a total budget holds across runs, evaluations, and the web UI."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

_LOCK = threading.Lock()


class SpendLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict:
        if not self.path.is_file():
            return {"total_usd": 0.0, "entries": []}
        return json.loads(self.path.read_text())

    def spent_usd(self) -> float:
        return round(self._load()["total_usd"], 6)

    def remaining_usd(self, total_cap_usd: float) -> float:
        return round(max(0.0, total_cap_usd - self.spent_usd()), 6)

    def record(self, run_id: str, provider: str, model: str, usd: float, status: str) -> None:
        if usd <= 0:
            return
        with _LOCK:
            doc = self._load()
            doc["entries"].append({"run_id": run_id, "provider": provider, "model": model, "usd": round(usd, 6),
                                   "status": status, "at": datetime.now(UTC).isoformat()})
            doc["total_usd"] = round(doc["total_usd"] + usd, 6)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(doc, indent=2))
