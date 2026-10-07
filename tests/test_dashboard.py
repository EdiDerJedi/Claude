"""Dashboard: Bot-Status, Momentaufnahmen und der lokale Webserver inkl. Sicherheitsprüfungen."""

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    from tradingai.status import InstanceLock

    assert read_bot_state(tmp_path)["state"] == "never"
    _heartbeat(tmp_path)
    assert read_bot_state(tmp_path)["state"] == "running"
    _heartbeat(tmp_path, phase="wartet auf MetaTrader 5", last_error="IPC timeout")
    st = read_bot_state(tmp_path)
    assert st["state"] == "waiting" and st["last_error"] == "IPC timeout"
    long_ago = iso(datetime.now(timezone.utc) - timedelta(hours=2))
    _heartbeat(tmp_path, started_at=long_ago, last_loop=long_ago)  # Herzschlag ja, Hauptschleife steht
    assert read_bot_state(tmp_path)["state"] == "stalled"
    old = iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    running = InstanceLock(tmp_path)
    assert running.acquire()  # ein Bot hält die Sperre, meldet sich aber nicht
    _heartbeat(tmp_path, time=old)
    assert read_bot_state(tmp_path)["state"] == "unresponsive"
    running.release()
    # Sperre frei -> Bot ist tot, egal was die (evtl. wiederverwendete) PID sagt
    request_stop(tmp_path)
    st = read_bot_state(tmp_path)
    assert st["state"] == "crashed" and not (tmp_path / "stop.request").exists()
    _heartbeat(tmp_path, stopped=True)
    assert read_bot_state(tmp_path)["state"] == "stopped"
    _heartbeat(tmp_path, stopped=True, error=True, phase="Echtgeld-Sperre ausgelöst")
    assert read_bot_state(tmp_path)["state"] == "stopped_error"


def test_bot_status_heartbeat_stop_request_and_equity(tmp_path):
    request_stop(tmp_path)
    os.utime(tmp_path / "stop.request", (0, 0))  # liegengebliebene Anfrage aus einem früheren Lauf
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


def test_stop_during_startup_is_honoured(tmp_path, monkeypatch):
    # Dashboard startet den Bot und der Nutzer klickt sofort "Bot stoppen"
    monkeypatch.setenv("TRADINGAI_SPAWNED_AT", repr(time.time() - 5))
    request_stop(tmp_path)  # kam nach dem Start, aber bevor der Bot geladen war
    status = BotStatus(tmp_path, "paper")
    status.start()
    assert status.stop_requested()
    status.stop()


def test_run_exits_immediately_when_stop_requested_before_start(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"symbols: [EURUSD]\nstate_dir: {state}\ninternet: {{enabled: false}}\n"
                        f"paper: {{data_source: csv, csv_dir: {tmp_path / 'fehlt'}}}\n")
    monkeypatch.setenv("TRADINGAI_SPAWNED_AT", repr(time.time() - 5))
    request_stop(state)
    t0 = time.time()
    assert main(["-c", str(cfg_file), "run"]) == 0
    assert time.time() - t0 < 10  # keine Verbindungsversuche, kein Handel
    hb = json.loads((state / "heartbeat.json").read_text())
    assert hb["stopped"] is True and hb["error"] is False


def test_account_switch_starts_statistics_fresh(cfg, tmp_path):
    df = synthetic_ohlc(800, "H1", seed=2)
    broker = BacktestBroker({"EURUSD": df}, {"EURUSD": default_symbol_info("EURUSD")}, 100_000, start=600)
    store = StateStore(tmp_path)
    (tmp_path / "trades.csv").write_text("ticket,profit\n1,5\n")
    (tmp_path / "equity_history.csv").write_text("time,balance,equity\n")
    store.data["account_key"] = "111@Alt-Server"
    store.data["risk"].update(peak_equity=1_000_000, day_start_equity=1_000_000, day="2026-10-07")
    cfg.mode = "paper"
    engine = TradingEngine(cfg, broker, store, learn=False, raise_errors=True)
    engine.step()
    assert store.data["account_key"] == "paper"
    assert store.data["risk"]["peak_equity"] == pytest.approx(100_000, rel=0.01)  # kein Not-Aus durch altes Konto
    assert not store.data["risk"]["killed"]
    assert not (tmp_path / "trades.csv").exists()
    archived = list((tmp_path / "archiv").glob("111_Alt_Server_*/trades.csv"))
    assert archived and store.data["account_notice"]["from"] == "111@Alt-Server"


def test_equity_downsampling_keeps_dips_and_filters_range(tmp_path):
    path = tmp_path / "equity_history.csv"
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    lines = ["time,balance,equity"]
    for i in range(30 * 288):  # 30 Tage alle 5 Minuten
        t = start + timedelta(minutes=5 * i)
        eq = 93_000 if i == 5000 else 100_000 + (i % 7)
        lines.append(f"{iso(t)},100000,{eq}")
    path.write_text("\n".join(lines) + "\n")
    reader = dash_mod.EquityReader(path)
    full = reader.series("all")
    assert len(full["points"]) <= 502 and min(p["equity"] for p in full["points"]) == 93_000
    week = reader.series("7d")
    first = datetime.fromisoformat(week["first"].replace("Z", "+00:00"))
    assert first >= start + timedelta(days=22)
    assert reader.series("unsinn")["range"] == "7d"


def test_dashboard_reloads_changed_config(server):
    d, base, state = server
    cfg_path = Path(d.config_path)
    cfg_path.write_text("symbols: [EURUSD.m, XAUUSD.m]\n")
    os.utime(cfg_path, (time.time() + 5, time.time() + 5))
    data = json.loads(_req(base + "/api/status")[2])
    assert data["config"]["symbols"] == ["EURUSD.m", "XAUUSD.m"]
    cfg_path.write_text("mode: kaputt\n")
    os.utime(cfg_path, (time.time() + 10, time.time() + 10))
    data = json.loads(_req(base + "/api/status")[2])
    assert "mode" in data["config"]["error"]
    assert _req(base + "/api/bot/start", "POST", {"X-TradingAI-Token": d.token})[0] == 400


def test_failed_start_only_shows_this_launch_and_running_heartbeat_wins(server, monkeypatch):
    d, base, state = server
    (state / "bot_stderr.log").write_text("alter Fehler von gestern\n")

    class DeadProc:
        pid = 1

        def poll(self):
            return 1

    def fake_popen(cmd, **kwargs):
        kwargs["stderr"].write(b"ValueError: mode muss paper oder live sein\n")
        return DeadProc()

    monkeypatch.setattr(dash_mod.subprocess, "Popen", fake_popen)
    assert _req(base + "/api/bot/start", "POST", {"X-TradingAI-Token": d.token})[0] == 202
    bot = json.loads(_req(base + "/api/status")[2])["bot"]
    assert bot["state"] == "failed" and bot["details"] == ["ValueError: mode muss paper oder live sein"]
    _heartbeat(state, launch_id="anderer-start")  # Bot läuft inzwischen (z.B. über bot_starten.bat)
    assert json.loads(_req(base + "/api/status")[2])["bot"]["state"] == "running"


def test_own_launch_recognised_by_launch_id(server, monkeypatch):
    d, base, state = server

    class LauncherProc:  # unter Windows: venv-Starter mit anderer PID als der eigentliche Bot
        pid = 999_999

        def poll(self):
            return None

    monkeypatch.setattr(dash_mod.subprocess, "Popen", lambda cmd, **kw: LauncherProc())
    _req(base + "/api/bot/start", "POST", {"X-TradingAI-Token": d.token})
    assert json.loads(_req(base + "/api/status")[2])["bot"]["state"] == "starting"
    _heartbeat(state, launch_id=d.launch_id, pid=12345)
    assert json.loads(_req(base + "/api/status")[2])["bot"]["state"] == "running"


def test_reset_kill_switch_updates_snapshot(server):
    d, base, state = server
    store = StateStore(state)
    store.data["risk"].update(killed=True, kill_reason="Drawdown")
    store.save()
    write_json_atomic(state / "status.json", {"risk": {"killed": True, "kill_reason": "Drawdown"}})
    assert _req(base + "/api/killswitch/reset", "POST", {"X-TradingAI-Token": d.token})[0] == 200
    assert json.loads(_req(base + "/api/status")[2])["snapshot"]["risk"]["killed"] is False


def test_settings_and_bad_requests(server):
    d, base, state = server
    tok = {"X-TradingAI-Token": d.token}
    req = urllib.request.Request(base + "/api/settings", method="POST", data=b'{"autostart_bot": true}',
                                 headers={**tok, "Content-Type": "application/json"})
    with _LOCAL.open(req, timeout=5) as resp:
        assert resp.status == 200
    assert json.loads(_req(base + "/api/status")[2])["prefs"]["autostart_bot"] is True
    if os.name != "nt":
        req = urllib.request.Request(base + "/api/settings", method="POST", data=b'{"autostart_windows": true}',
                                     headers=tok)
        try:
            _LOCAL.open(req, timeout=5)
            raise AssertionError("sollte 400 liefern")
        except urllib.error.HTTPError as err:
            assert err.code == 400
    assert _req(base + "/api/bot/stop", "POST", {"X-TradingAI-Token": "äöü".encode().decode("latin-1")})[0] == 403


def test_dashboard_refuses_non_local_host(tmp_path, monkeypatch):
    cfg = Config()
    cfg.state_dir = str(tmp_path / "state")
    cfg.dashboard.host = "0.0.0.0"
    cfg.dashboard.port = _free_port()
    d = dash_mod.Dashboard(cfg, None, cwd=tmp_path)
    t = threading.Thread(target=d.serve, kwargs={"open_browser": False}, daemon=True)
    t.start()
    for _ in range(50):
        if d.server is not None:
            break
        time.sleep(0.05)
    try:
        assert d.server.server_address[0] == "127.0.0.1"
    finally:
        d.server.shutdown()
