"""Dashboard: Bot-Status, Momentaufnahmen und der lokale Webserver inkl. Sicherheitsprüfungen."""

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from tradingai import dashboard as dash_mod
from tradingai.brokers.sim import BacktestBroker
from tradingai.cli import main
from tradingai.config import Config
from tradingai.data.market_data import synthetic_ohlc
from tradingai.data.symbols import default_symbol_info
from tradingai.engine import TradingEngine
from tradingai.state import StateStore
from tradingai.status import BotStatus, iso, read_bot_state, request_stop, write_json_atomic


# ------------------------------------------------------------------ Status
def _heartbeat(state, **kw):
    data = {"pid": os.getpid(), "time": iso(datetime.now(timezone.utc)), "phase": "wartet", "stopped": False}
    data.update(kw)
    write_json_atomic(state / "heartbeat.json", data)


def test_bot_states(tmp_path):
    assert read_bot_state(tmp_path)["state"] == "never"
    _heartbeat(tmp_path)
    assert read_bot_state(tmp_path)["state"] == "running"
    old = iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    _heartbeat(tmp_path, time=old)  # eigener Prozess lebt -> reagiert nicht
    assert read_bot_state(tmp_path)["state"] == "unresponsive"
    _heartbeat(tmp_path, time=old, pid=2_000_000_000)  # Prozess existiert nicht mehr
    assert read_bot_state(tmp_path)["state"] == "crashed"
    _heartbeat(tmp_path, stopped=True)
    assert read_bot_state(tmp_path)["state"] == "stopped"


def test_bot_status_heartbeat_stop_request_and_equity(tmp_path):
    request_stop(tmp_path)  # liegengebliebene Anfrage darf neuen Bot nicht stoppen
    status = BotStatus(tmp_path, "paper", interval=0.05, equity_every=300)
    status.start()
    assert not status.stop_requested()
    status.phase = "lernt EURUSD"
    time.sleep(0.2)
    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["phase"] == "lernt EURUSD" and not hb["stopped"]
    request_stop(tmp_path)
    assert status.stop_requested() and not status.stop_requested()
    t0 = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    assert status.record_equity(1000, 1000, t0)
    assert not status.record_equity(1000, 1010, t0 + timedelta(minutes=1))  # zu früh, Kontostand gleich
    assert status.record_equity(1050, 1050, t0 + timedelta(minutes=2))  # Trade geschlossen -> sofort
    assert status.record_equity(1050, 1060, t0 + timedelta(minutes=8))
    status.stop("beendet")
    assert json.loads((tmp_path / "heartbeat.json").read_text())["stopped"] is True
    assert len((tmp_path / "equity_history.csv").read_text().strip().splitlines()) == 4


def test_engine_snapshot(cfg):
    df = synthetic_ohlc(800, "H1", seed=2)
    broker = BacktestBroker({"EURUSD": df}, {"EURUSD": default_symbol_info("EURUSD")}, 10_000, start=600)
    engine = TradingEngine(cfg, broker, StateStore(None), learn=False, raise_errors=True)
    engine.step()
    broker.open_position("EURUSD", 1, 0.1, 1.0, 2.0)
    snap = engine.snapshot()
    assert snap["account"]["currency"] and snap["account"]["server"] == "Simulation"
    assert snap["positions"][0]["symbol"] == "EURUSD"
    assert snap["risk"]["limits"]["max_drawdown"] == cfg.risk.max_drawdown
    json.dumps(snap)  # muss als JSON speicherbar sein


def test_run_once_publishes_status(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    synthetic_ohlc(1300, "H1", seed=4).to_csv(data / "EURUSD_H1.csv")
    state = tmp_path / "state"
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"symbols: [EURUSD]\nstate_dir: {state}\ninternet: {{enabled: false}}\n"
                        f"learning: {{enabled: false}}\npaper: {{data_source: csv, csv_dir: {data}}}\n")
    assert main(["-c", str(cfg_file), "run", "--once"]) == 0
    assert json.loads((state / "status.json").read_text())["account"]["balance"] == 10_000
    assert json.loads((state / "heartbeat.json").read_text())["stopped"] is True
    assert (state / "equity_history.csv").exists()


# ------------------------------------------------------------------ Webserver
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path, monkeypatch):
    cfg = Config()
    cfg.symbols = ["EURUSD"]
    cfg.state_dir = str(tmp_path / "state")
    (tmp_path / "state").mkdir()
    cfg.dashboard.port = _free_port()
    (tmp_path / "config.yaml").write_text("symbols: [EURUSD]\n")
    d = dash_mod.Dashboard(cfg, str(tmp_path / "config.yaml"), cwd=tmp_path)
    t = threading.Thread(target=d.serve, kwargs={"open_browser": False}, daemon=True)
    t.start()
    for _ in range(50):
        if d.server is not None:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{d.server.server_address[1]}"
    yield d, base, tmp_path / "state"
    d.server.shutdown()


_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _req(url, method="GET", headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=b"{}" if method == "POST" else None)
    try:
        with _LOCAL.open(req, timeout=5) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read().decode("utf-8")


def test_page_has_token_and_strict_csp(server):
    d, base, _ = server
    code, headers, body = _req(base + "/")
    assert code == 200
    assert d.token in body and "__TOKEN__" not in body
    csp = headers["Content-Security-Policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in body
    assert "default-src 'none'" in csp


def test_status_api(server, tmp_path):
    d, base, state = server
    code, _, body = _req(base + "/api/status")
    data = json.loads(body)
    assert code == 200 and data["bot"]["state"] == "never" and data["trades"]["stats"]["count"] == 0
    (state / "trades.csv").write_text("ticket,symbol,direction,volume,open_price,close_price,open_time,close_time,"
                                      "profit,reason,comment\n1,EURUSD,1,0.1,1.1,1.2,a,b,50,tp,\n"
                                      "2,EURUSD,-1,0.1,1.1,1.2,a,b,-20,sl,\n")
    data = json.loads(_req(base + "/api/status")[2])
    assert data["trades"]["stats"]["count"] == 2 and data["trades"]["recent"][0]["reason"] == "Stop-Loss"
    assert data["trades"]["stats"]["profit_factor"] == pytest.approx(2.5)


def test_actions_need_token_host_and_origin(server):
    d, base, state = server
    assert _req(base + "/api/bot/stop", "POST")[0] == 403  # ohne Token
    tok = {"X-TradingAI-Token": d.token}
    assert _req(base + "/api/bot/stop", "POST", {**tok, "Origin": "http://evil.example"})[0] == 403
    port = d.server.server_address[1]
    assert _req(base + "/api/status", headers={"Host": f"evil.example:{port}"})[0] == 403  # DNS-Rebinding
    assert _req(base + "/api/bot/stop", "POST", tok)[0] == 409  # Bot läuft nicht
    _heartbeat(state)
    assert _req(base + "/api/bot/stop", "POST", tok)[0] == 202
    assert (state / "stop.request").exists()


def test_start_bot_launches_hidden_process(server, monkeypatch):
    d, base, state = server
    calls = []

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeProc()

    monkeypatch.setattr(dash_mod.subprocess, "Popen", fake_popen)
    code, _, body = _req(base + "/api/bot/start", "POST", {"X-TradingAI-Token": d.token})
    assert code == 202
    cmd, kwargs = calls[0]
    assert cmd[-1] == "run" and "-m" in cmd and kwargs["stdin"] is not None
    assert json.loads(_req(base + "/api/status")[2])["bot"]["state"] == "starting"
    assert _req(base + "/api/bot/start", "POST", {"X-TradingAI-Token": d.token})[0] == 409  # nicht doppelt


def test_killswitch_reset_only_when_bot_stopped(server):
    from tradingai.cli import InstanceLock

    d, base, state = server
    store = StateStore(state)
    store.data["risk"].update(killed=True, kill_reason="Test")
    store.save()
    running = InstanceLock(str(state))
    assert running.acquire()
    tok = {"X-TradingAI-Token": d.token}
    assert _req(base + "/api/killswitch/reset", "POST", tok)[0] == 409
    running.release()
    assert _req(base + "/api/killswitch/reset", "POST", tok)[0] == 200
    assert StateStore(state).data["risk"]["killed"] is False


def test_second_dashboard_reuses_running_one(server, monkeypatch):
    d, base, _ = server
    opened = []
    monkeypatch.setattr(dash_mod.webbrowser, "open", lambda url: opened.append(url))
    cfg = Config()
    cfg.dashboard.port = d.server.server_address[1]
    other = dash_mod.Dashboard(cfg, None)
    other.serve(open_browser=True)  # kehrt sofort zurück, weil schon ein Dashboard läuft
    assert opened == [base + "/"] and other.server is None
