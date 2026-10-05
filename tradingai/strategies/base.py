"""Basisklasse für Handelsstrategien."""

from abc import ABC, abstractmethod

import numpy as np
import pandas as pd


class Strategy(ABC):
    """Eine Strategie liefert pro Bar eine Zielposition: +1 Long, -1 Short, 0 flat.

    generate() muss vektorisiert über den ganzen DataFrame arbeiten und darf an
    Bar t nur Daten bis einschließlich t verwenden.
    """

    name: str = "base"
    default_params: dict = {}
    # Suchraum für den Optimierer: name -> (min, max). Ganzzahlige Grenzen = int-Parameter.
    param_space: dict = {}

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params, **(params or {})}

    @abstractmethod
    def generate(self, df: pd.DataFrame) -> pd.Series: ...

    def is_valid(self, params: dict) -> bool:
        return True

    def sample_params(self, rng: np.random.Generator) -> dict:
        out = {}
        for key, (low, high) in self.param_space.items():
            if isinstance(low, int) and isinstance(high, int):
                out[key] = int(rng.integers(low, high + 1))
            else:
                out[key] = round(float(rng.uniform(low, high)), 3)
        return out

    def with_params(self, params: dict) -> "Strategy":
        return type(self)(params)

    @staticmethod
    def _series(values, index) -> pd.Series:
        return pd.Series(np.asarray(values, dtype=float), index=index).fillna(0.0)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.params})"
