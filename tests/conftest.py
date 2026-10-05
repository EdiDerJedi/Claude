import pytest

from tradingai.config import Config
from tradingai.data.market_data import synthetic_ohlc


@pytest.fixture
def ohlc():
    return synthetic_ohlc(1500, "H1", seed=3)


@pytest.fixture
def cfg():
    c = Config()
    c.symbols = ["EURUSD"]
    c.history_bars = 400
    c.learning.train_bars = 1500
    c.learning.min_train_bars = 800
    c.learning.optimize_trials = 8
    c.internet.enabled = False
    return c
