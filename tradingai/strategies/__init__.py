from .base import Strategy
from .breakout import BreakoutStrategy
from .mean_reversion import MeanReversionStrategy
from .ml import MLStrategy
from .trend import TrendStrategy

RULE_STRATEGIES = {cls.name: cls for cls in (TrendStrategy, MeanReversionStrategy, BreakoutStrategy)}

__all__ = [
    "Strategy",
    "TrendStrategy",
    "MeanReversionStrategy",
    "BreakoutStrategy",
    "MLStrategy",
    "RULE_STRATEGIES",
]
