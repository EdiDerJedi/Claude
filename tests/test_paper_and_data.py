import pandas as pd

from tradingai.brokers.sim import PaperBroker
from tradingai.cli import main
from tradingai.data.market_data import load_csv, synthetic_ohlc
from tradingai.data.symbols import default_symbol_info
from tradingai.engine import TradingEngine
from tradingai.state import StateStore


def test_load_mt5_export(tmp_path):
    path = tmp_path / "EURUSD_H1.csv"
    path.write_text(
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
        "2024.01.02\t00:00:00\t1.10427\t1.10450\t1.10380\t1.10400\t1200\t0\t5\n"
        "2024.01.02\t01:00:00\t1.10400\t1.10480\t1.10390\t1.10470\t900\t0\t5\n"
    )
    df = load_csv(path)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index[1] == pd.Timestamp("2024-01-02 01:00")
    assert df["volume"].iloc[0] == 1200


def test_paper_broker_trades_and_survives_restart(cfg, tmp_path):
    full = synthetic_ohlc(900, "H1", seed=21)
    feed = {"n": 700}

    def fetch(symbol):
        return full.iloc[: feed["n"]]

    infos = {"EURUSD": default_symbol_info("EURUSD")}
    state_file = tmp_path / "paper_account.json"
    broker = PaperBroker(infos, 10_000, state_file, fetch, refresh_seconds=0)
    engine = TradingEngine(cfg, broker, StateStore(tmp_path), learn=False, raise_errors=True)
    for _ in range(150):
        engine.step()
        feed["n"] += 1  # eine neue Kerze kommt "aus dem Internet"
    assert state_file.exists()
    activity = len(broker.sim.closed) + len(broker.sim.positions)
    assert activity > 0

    restarted = PaperBroker(infos, 10_000, state_file, fetch, refresh_seconds=0)
    assert restarted.sim.balance == broker.sim.balance
    assert len(restarted.sim.positions) == len(broker.sim.positions)


def test_cli_status_and_reset(tmp_path, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"symbols: [EURUSD]\nstate_dir: {tmp_path / 'state'}\n")
    store = StateStore(tmp_path / "state")
    store.data["risk"].update(killed=True, kill_reason="Test", equity=9000, peak_equity=10600)
    store.save()
    assert main(["-c", str(cfg_file), "status"]) == 0
    assert "AKTIV" in capsys.readouterr().out
    assert main(["-c", str(cfg_file), "reset-killswitch"]) == 0
    assert StateStore(tmp_path / "state").data["risk"]["killed"] is False


def test_load_german_excel_and_utf16_csv(tmp_path):
    german = tmp_path / "de.csv"
    german.write_text("Datum;Open;High;Low;Close\n02.01.2024;1,1043;1,1045;1,1038;1,1040\n"
                      "03.01.2024;1,1040;1,1050;1,1030;1,1045\n".replace("Datum", "date"), encoding="utf-8")
    df = load_csv(german)
    assert df.index[1] == pd.Timestamp("2024-01-03") and df["close"].iloc[0] == 1.104
    utf16 = tmp_path / "u16.csv"
    utf16.write_text("time,open,high,low,close\n2024-01-02 00:00,1,2,0.5,1.5\n", encoding="utf-16")
    assert load_csv(utf16)["high"].iloc[0] == 2.0


def test_second_bot_instance_is_refused(tmp_path):
    from tradingai.cli import InstanceLock

    a, b = InstanceLock(str(tmp_path)), InstanceLock(str(tmp_path))
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_cli_reset_refused_while_bot_runs(tmp_path):
    import pytest

    from tradingai.cli import InstanceLock

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"symbols: [EURUSD]\nstate_dir: {tmp_path / 'state'}\n")
    running = InstanceLock(str(tmp_path / "state"))
    assert running.acquire()
    with pytest.raises(SystemExit, match="läuft bereits"):
        main(["-c", str(cfg_file), "reset-killswitch"])
    running.release()
