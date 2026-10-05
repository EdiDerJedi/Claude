"""Dauerhafter Zustand: Gelerntes, Parameter, Modelle, Risiko-Status und Trade-Journal."""

import csv
import json
import logging
import os
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


class StateStore:
    """Speichert alles unter state_dir. Mit state_dir=None nur im Speicher (Backtests)."""

    def __init__(self, state_dir: str | Path | None):
        self.dir = Path(state_dir) if state_dir else None
        self.data: dict = {}
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "models").mkdir(exist_ok=True)
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
        for key in ("params", "last_optimize", "last_retrain", "last_bar", "risk", "decisions", "model_metrics"):
            self.data.setdefault(key, {})
        self.memory_models: dict = {}

    @property
    def path(self) -> Path:
        return self.dir / "state.json"

    def save(self) -> None:
        if not self.dir:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------- Zeiten
    def get_time(self, key: str, symbol: str) -> pd.Timestamp | None:
        v = self.data[key].get(symbol)
        return pd.Timestamp(v) if v else None

    def set_time(self, key: str, symbol: str, value) -> None:
        self.data[key][symbol] = pd.Timestamp(value).isoformat()

    # ---------------------------------------------------------- Parameter
    def params(self, symbol: str, strategy: str) -> dict | None:
        return self.data["params"].get(symbol, {}).get(strategy)

    def set_params(self, symbol: str, strategy: str, params: dict) -> None:
        self.data["params"].setdefault(symbol, {})[strategy] = params

    # ------------------------------------------------------------- Modelle
    def model_path(self, symbol: str) -> Path | None:
        if not self.dir:
            return None
        safe = "".join(ch if ch.isalnum() else "_" for ch in symbol)
        return self.dir / "models" / f"{safe}.joblib"

    def save_model(self, symbol: str, bundle) -> None:
        self.memory_models[symbol] = bundle
        path = self.model_path(symbol)
        if path:
            import joblib

            joblib.dump(bundle, path)

    def load_model(self, symbol: str):
        if symbol in self.memory_models:
            return self.memory_models[symbol]
        path = self.model_path(symbol)
        if path and path.exists():
            import joblib

            try:
                bundle = joblib.load(path)
                self.memory_models[symbol] = bundle
                return bundle
            except Exception as exc:
                log.warning("Modell %s konnte nicht geladen werden (%s) – wird neu trainiert", path, exc)
        return None

    # ------------------------------------------------------------- Journal
    def append_trades(self, trades) -> None:
        if not self.dir or not trades:
            return
        path = self.dir / "trades.csv"
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(["ticket", "symbol", "direction", "volume", "open_price", "close_price",
                                 "open_time", "close_time", "profit", "reason", "comment"])
            for t in trades:
                writer.writerow([t.ticket, t.symbol, t.direction, t.volume, t.open_price, t.close_price,
                                 t.open_time, t.close_time, round(t.profit, 2), t.reason, t.comment])
