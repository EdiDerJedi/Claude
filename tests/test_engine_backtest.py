import pandas as pd

from tradingai.backtest import run_backtest
from tradingai.brokers.sim import BacktestBroker
from tradingai.cli import main
from tradingai.data.market_data import synthetic_ohlc
from tradingai.data.symbols import default_symbol_info
from tradingai.engine import TradingEngine
from tradingai.state import StateStore


def test_backtest_runs_and_learns(cfg):
    data = {"EURUSD": synthetic_ohlc(1400, "H1", seed=5)}
    report = run_backtest(cfg, data)
    s = report.stats
    assert s["trades"] > 0
    assert s["final"] > 0
    assert 0 <= s["max_drawdown"] < 1
    assert "EURUSD" in report.state["decisions"]
    assert report.state["last_optimize"]["EURUSD"]
    assert "EURUSD" in report.state["model_metrics"]
    # Jede Position hatte einen Stop-Loss -> kein Einzelverlust deutlich über dem Risiko
    worst = min(t.profit for t in report.trades)
    assert worst > -cfg.paper.initial_balance * cfg.risk.risk_per_trade * 3
    assert "BACKTEST-ERGEBNIS" in report.format()


def test_backtest_multiple_symbols_without_learning(cfg):
    data = {"EURUSD": synthetic_ohlc(900, "H1", seed=1), "XAUUSD": synthetic_ohlc(900, "H1", seed=2,
                                                                                  start_price=2300.0)}
    report = run_backtest(cfg, data, learn=False)
    assert set(report.per_symbol) == {"EURUSD", "XAUUSD"}
    assert len(report.equity) > 400


def test_kill_switch_closes_everything(cfg):
    df = synthetic_ohlc(800, "H1", seed=4)
    broker = BacktestBroker({"EURUSD": df}, {"EURUSD": default_symbol_info("EURUSD")}, 10_000, start=600)
    store = StateStore(None)
    engine = TradingEngine(cfg, broker, store, learn=False, raise_errors=True)
    broker.open_position("EURUSD", 1, 1.0, 0.0, 0.0)
    store.data["risk"].update(killed=True, kill_reason="Test")
    broker.advance()
    engine.step()
    assert broker.positions() == []
    assert not store.data["decisions"]["EURUSD"]["action"].startswith(("KAUF", "VERKAUF"))


def test_state_persists_between_restarts(cfg, tmp_path):
    df = synthetic_ohlc(1300, "H1", seed=6)
    infos = {"EURUSD": default_symbol_info("EURUSD")}
    broker = BacktestBroker({"EURUSD": df}, infos, 10_000, start=1000)
    store = StateStore(tmp_path)
    engine = TradingEngine(cfg, broker, store, raise_errors=True, rng_seed=1)
    for _ in range(5):
        engine.step()
        broker.advance()
    assert (tmp_path / "state.json").exists()

    # "Neustart": neue Engine mit demselben Ordner übernimmt Gelerntes
    store2 = StateStore(tmp_path)
    engine2 = TradingEngine(cfg, broker, store2, raise_errors=True)
    assert engine2.selector.to_dict() == engine.selector.to_dict()
    for name, params in store2.data["params"].get("EURUSD", {}).items():
        assert engine2.strategies["EURUSD"][name].params == params
    if store2.data["model_metrics"]["EURUSD"]["accepted"]:
        assert engine2.strategies["EURUSD"]["ml"].bundle is not None


def test_engine_respects_calendar_block(cfg):
    from datetime import datetime, timezone

    class AlwaysBlocked:
        def update(self):
            pass

        def blocking_event(self, currencies, now):
            return type("E", (), {"currency": "USD", "title": "NFP", "time": datetime.now(timezone.utc)})()

    df = synthetic_ohlc(1000, "H1", seed=8)
    broker = BacktestBroker({"EURUSD": df}, {"EURUSD": default_symbol_info("EURUSD")}, 10_000, start=500)
    engine = TradingEngine(cfg, broker, StateStore(None), calendar=AlwaysBlocked(), learn=False, raise_errors=True)
    for _ in range(300):
        engine.step()
        broker.advance()
    assert broker.sim.closed == [] and broker.positions() == []


def test_cli_backtest_synthetic(tmp_path, capsys):
    rc = main(["-c", str(tmp_path / "missing.yaml"), "backtest", "--source", "synthetic", "--bars", "900",
               "--no-learn", "--out", str(tmp_path / "out"), "-q"])
    assert rc == 0
    assert "BACKTEST-ERGEBNIS" in capsys.readouterr().out
    trades = pd.read_csv(tmp_path / "out" / "equity.csv")
    assert len(trades) > 100


def test_learning_retries_when_history_is_still_loading(cfg):
    # MT5 liefert beim ersten Start oft noch wenig Historie: dann nicht eine Woche warten
    df = synthetic_ohlc(1200, "H1", seed=9)
    broker = BacktestBroker({"EURUSD": df}, {"EURUSD": default_symbol_info("EURUSD")}, 10_000, start=500)
    store = StateStore(None)
    engine = TradingEngine(cfg, broker, store, learn=True, raise_errors=True, rng_seed=0)
    engine.step()  # nur 501 Bars < min_train_bars (800)
    assert "EURUSD" not in store.data["last_optimize"]
    while broker.i < 820:
        broker.advance()
    engine.step()
    assert "EURUSD" in store.data["last_optimize"]
    assert "EURUSD" in store.data["model_metrics"]


def test_config_with_utf8_bom(tmp_path):
    from tradingai.config import load_config

    path = tmp_path / "config.yaml"
    path.write_bytes("﻿mode: paper\nsymbols: [GBPUSD]\n".encode("utf-8"))
    assert load_config(path).symbols == ["GBPUSD"]
