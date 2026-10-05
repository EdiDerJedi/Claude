import numpy as np
import pytest

from tradingai.strategies import BreakoutStrategy, MeanReversionStrategy, TrendStrategy


@pytest.mark.parametrize("cls", [TrendStrategy, MeanReversionStrategy, BreakoutStrategy])
def test_signals_valid_and_causal(cls, ohlc):
    s = cls()
    sig = s.generate(ohlc)
    assert len(sig) == len(ohlc)
    assert set(np.unique(sig)) <= {-1.0, 0.0, 1.0}
    assert (sig != 0).any(), "Strategie sollte auf 1500 Bars mindestens einmal handeln"
    for cut in (400, 777, 1200):
        assert s.generate(ohlc.iloc[:cut]).iloc[-1] == sig.iloc[cut - 1]


@pytest.mark.parametrize("cls", [TrendStrategy, MeanReversionStrategy, BreakoutStrategy])
def test_sampled_params_in_space(cls):
    s = cls()
    rng = np.random.default_rng(0)
    for _ in range(20):
        p = s.sample_params(rng)
        for key, (low, high) in s.param_space.items():
            assert low <= p[key] <= high
