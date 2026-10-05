from datetime import datetime

import pytest

from tradingai.brokers.sim import SimulatedAccount
from tradingai.config import RiskConfig
from tradingai.data.symbols import base_symbol, calendar_currencies, default_symbol_info, sentiment_legs, yahoo_ticker
from tradingai.risk import RiskManager

T0 = datetime(2024, 1, 2, 10)


def test_position_size_risks_one_percent():
    info = default_symbol_info("EURUSD")
    rm = RiskManager(RiskConfig(risk_per_trade=0.01), {})
    # 20 Pips Stop bei 10.000 USD: 100 USD Risiko / (200 Ticks * 1 USD) = 0.5 Lot
    assert rm.position_size(10_000, 0.0020, info) == pytest.approx(0.5)


def test_position_size_zero_when_account_too_small():
    info = default_symbol_info("EURUSD")
    rm = RiskManager(RiskConfig(risk_per_trade=0.01), {})
    assert rm.position_size(100, 0.0100, info) == 0.0


def test_daily_loss_limit_and_kill_switch():
    state = {}
    rm = RiskManager(RiskConfig(max_daily_loss=0.03, max_drawdown=0.10, max_open_positions=2), state)
    rm.update_equity(10_000, T0)
    assert rm.can_open(0)[0]
    assert not rm.can_open(2)[0]
    rm.update_equity(9_650, T0)
    ok, reason = rm.can_open(0)
    assert not ok and "Tagesverlust" in reason
    rm.update_equity(9_650, datetime(2024, 1, 3, 10))  # neuer Tag
    assert rm.can_open(0)[0]
    rm.update_equity(8_900, datetime(2024, 1, 3, 11))
    assert rm.killed and not rm.can_open(0)[0]
    rm.update_equity(9_500, datetime(2024, 1, 4, 11))
    assert rm.killed, "Not-Aus bleibt aktiv bis zum manuellen Reset"
    rm.reset_kill_switch()
    assert not rm.killed


def _account():
    return SimulatedAccount(10_000, {"EURUSD": default_symbol_info("EURUSD")})


def test_sim_long_take_profit_and_spread():
    acc = _account()
    pos = acc.open("EURUSD", 1, 1.0, 1.1000, T0, sl=1.0950, tp=1.1050)
    assert pos.open_price == pytest.approx(1.10005)  # Kauf zum Ask (halber Spread)
    acc.on_bar("EURUSD", T0, 1.1010, 1.1060, 1.1005, 1.1055)
    assert not acc.positions
    trade = acc.closed[-1]
    assert trade.reason == "tp" and trade.close_price == pytest.approx(1.1050)
    assert trade.profit == pytest.approx((1.1050 - 1.10005) / 0.00001 * 1.0)


def test_sim_stop_loss_wins_when_both_hit():
    acc = _account()
    acc.open("EURUSD", 1, 1.0, 1.1000, T0, sl=1.0950, tp=1.1050)
    acc.on_bar("EURUSD", T0, 1.1000, 1.1100, 1.0900, 1.1000)
    assert acc.closed[-1].reason == "sl"


def test_sim_short_gap_through_stop_fills_at_open():
    acc = _account()
    acc.open("EURUSD", -1, 1.0, 1.1000, T0, sl=1.1050, tp=1.0900)
    acc.on_bar("EURUSD", T0, 1.1100, 1.1120, 1.1080, 1.1090)
    t = acc.closed[-1]
    assert t.reason == "sl" and t.close_price == pytest.approx(1.11005)  # schlechter als SL: Kurslücke
    assert t.profit < 0


def test_sim_equity_and_persistence():
    acc = _account()
    acc.open("EURUSD", 1, 0.5, 1.1000, T0, sl=1.09, tp=1.12)
    acc.marks["EURUSD"] = 1.1020
    eq = acc.equity()
    clone = SimulatedAccount(0, acc.infos).load(acc.to_dict())
    assert clone.equity() == pytest.approx(eq)
    assert len(clone.positions) == 1


def test_symbol_helpers():
    assert base_symbol("EURUSD.m") == "EURUSD"
    assert base_symbol("EURUSDm") == "EURUSD"
    assert base_symbol("XAUUSD-ECN") == "XAUUSD"
    assert yahoo_ticker("EURUSD") == "EURUSD=X"
    assert yahoo_ticker("XAUUSD") == "GC=F"
    assert sentiment_legs("GBPJPY") == ("GBP", "JPY")
    assert sentiment_legs("US500") == ("EQ_US", None)
    assert calendar_currencies("XAUUSD") == ["USD"]
    assert calendar_currencies("EURUSD") == ["EUR", "USD"]
    assert calendar_currencies("GER40", {"GER40": ["EUR"]}) == ["EUR"]
