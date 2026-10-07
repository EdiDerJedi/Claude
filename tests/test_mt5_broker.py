"""Testet die MT5-Anbindung gegen ein nachgebautes MetaTrader5-Modul (MT5 läuft nur unter Windows)."""

from types import SimpleNamespace

import numpy as np
import pytest

from tradingai.brokers.mt5 import MT5Broker
from tradingai.config import MT5Config


class FakeMT5:
    TIMEFRAME_H1 = 16385
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC = 0
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL = 10009, 10010
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1, 0, 1

    def __init__(self, trade_mode=0, filling_mode=2, tick_size=0.00001):
        self.trade_mode = trade_mode
        self.filling_mode = filling_mode
        self.tick_size = tick_size
        self.requests = []
        self.init_args = None
        self.deals = [
            SimpleNamespace(ticket=50, magic=26100501, entry=1, type=1, position_id=1, symbol="EURUSD", volume=0.1,
                            price=1.105, time=1_700_000_500, profit=50.0, commission=-0.7, swap=-0.3, comment="tp"),
            SimpleNamespace(ticket=51, magic=0, entry=1, type=1, position_id=9, symbol="EURUSD", volume=1.0,
                            price=1.105, time=1_700_000_500, profit=10.0, commission=0, swap=0, comment=""),
        ]

    def initialize(self, *args, **kwargs):
        self.init_args = (args, kwargs)
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        return SimpleNamespace(login=123, server="Demo-Server", balance=10_000.0, equity=10_050.0, currency="USD",
                               leverage=100, trade_mode=self.trade_mode, margin_free=9_000.0)

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info(self, symbol):
        return SimpleNamespace(digits=5, point=0.00001, trade_tick_size=self.tick_size, trade_tick_value=1.0,
                               trade_contract_size=100_000, volume_min=0.01, volume_max=50.0, volume_step=0.01,
                               spread=12, trade_stops_level=10, filling_mode=self.filling_mode)

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=1.10000, ask=1.10012, time=0)

    def copy_rates_from_pos(self, symbol, tf, start, count):
        dtype = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                 ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]
        t0 = 1_700_000_000 - 1_700_000_000 % 3600
        rows = [(t0 + i * 3600, 1.1, 1.2, 1.0, 1.1 + i * 1e-4, 100, 10, 0) for i in range(count)]
        return np.array(rows, dtype=dtype)

    def positions_get(self, symbol=None):
        return [
            SimpleNamespace(ticket=1, symbol="EURUSD", type=0, volume=0.1, price_open=1.1, time=1_700_000_000,
                            sl=1.09, tp=1.12, profit=5.0, comment="tai", magic=26100501),
            SimpleNamespace(ticket=2, symbol="EURUSD", type=1, volume=1.0, price_open=1.1, time=1_700_000_000,
                            sl=0.0, tp=0.0, profit=-3.0, comment="manuell", magic=0),
        ]

    def order_send(self, request):
        self.requests.append(request)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=777, price=request.get("price", 0.0),
                               comment="done")

    def history_deals_get(self, date_from=None, date_to=None, position=None):
        if position is not None:
            return [SimpleNamespace(ticket=900 + position, entry=self.DEAL_ENTRY_IN, price=1.1, time=1_700_000_000,
                                    position_id=position)]
        return list(self.deals)


@pytest.fixture
def broker():
    fake = FakeMT5()
    b = MT5Broker(MT5Config(login=123, password="x", server="Demo-Server"), mt5_module=fake)
    b.connect()
    return b, fake


def test_connect_uses_credentials(broker):
    b, fake = broker
    assert fake.init_args[1] == {"login": 123, "password": "x", "server": "Demo-Server"}
    acc = b.account()
    assert acc.is_demo and acc.login == 123 and acc.server == "Demo-Server"


def test_rates_drop_unfinished_bar(broker):
    b, _ = broker
    df = b.get_rates("EURUSD", "H1", 50)
    assert len(df) == 50
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.is_monotonic_increasing


def test_only_own_positions(broker):
    b, _ = broker
    pos = b.positions("EURUSD")
    assert [p.ticket for p in pos] == [1]
    assert pos[0].direction == 1


def test_open_order_request(broker):
    b, fake = broker
    res = b.open_position("EURUSD", 1, 0.25, sl=1.09500, tp=1.10900, comment="tai +0.55 sehr langer Kommentar xyz")
    assert res.ok and res.ticket == 777
    req = fake.requests[-1]
    assert req["action"] == fake.TRADE_ACTION_DEAL
    assert req["type"] == fake.ORDER_TYPE_BUY
    assert req["price"] == 1.10012  # Kauf zum Ask
    assert req["volume"] == 0.25 and req["magic"] == 26100501
    assert req["type_filling"] == fake.ORDER_FILLING_IOC  # filling_mode-Bit 2 = IOC
    assert len(req["comment"]) <= 31


def test_stops_respect_broker_minimum_distance(broker):
    b, fake = broker
    b.open_position("EURUSD", -1, 0.1, sl=1.10005, tp=1.09999)
    req = fake.requests[-1]
    assert req["price"] == 1.10000  # Verkauf zum Bid
    # MT5 prüft Stops einer Short-Position gegen den Ask (1.10012), Mindestabstand 10 Points
    assert req["sl"] >= 1.10012 + 10 * 0.00001 - 1e-9
    assert req["tp"] <= 1.10012 - 10 * 0.00001 + 1e-9


def test_prices_rounded_to_tick_size():
    fake = FakeMT5(tick_size=0.00005)
    b = MT5Broker(MT5Config(), mt5_module=fake)
    b.open_position("EURUSD", 1, 0.1, sl=1.09123, tp=1.11177)
    req = fake.requests[-1]
    assert round(req["sl"] / 0.00005, 6).is_integer()
    assert round(req["tp"] / 0.00005, 6).is_integer()


def test_trailing_modify_skipped_when_too_close(broker):
    b, fake = broker
    p = b.positions("EURUSD")[0]  # Long, Bid 1.10000
    n = len(fake.requests)
    res = b.modify_position(p, 1.09995, p.tp)
    assert not res.ok and len(fake.requests) == n


def test_close_and_modify(broker):
    b, fake = broker
    p = b.positions("EURUSD")[0]
    assert b.close_position(p).ok
    req = fake.requests[-1]
    assert req["type"] == fake.ORDER_TYPE_SELL and req["position"] == 1 and req["price"] == 1.10000
    assert b.modify_position(p, 1.095, 1.12).ok
    assert fake.requests[-1]["action"] == fake.TRADE_ACTION_SLTP


def test_closed_trades_only_once_only_own_and_not_after_restart():
    fake = FakeMT5()
    journal = {}
    b = MT5Broker(MT5Config(), mt5_module=fake, journal_state=journal)
    assert b.pop_closed_trades() == []  # erster Start: alte Deals nur merken, nicht nachtragen
    fake.deals.append(SimpleNamespace(ticket=60, magic=26100501, entry=1, type=0, position_id=2, symbol="EURUSD",
                                      volume=0.2, price=1.099, time=1_700_090_000, profit=-20.0, commission=-1.0,
                                      swap=0.0, comment="sl"))
    trades = b.pop_closed_trades()
    assert len(trades) == 1 and trades[0].profit == pytest.approx(-21.0) and trades[0].direction == -1
    assert trades[0].open_price == 1.1  # Einstiegspreis aus dem Eröffnungs-Deal
    assert b.pop_closed_trades() == []
    # Neustart mit gespeichertem Journal-Zustand: nichts doppelt
    restarted = MT5Broker(MT5Config(), mt5_module=fake, journal_state=journal)
    assert restarted.pop_closed_trades() == []


def test_empty_password_and_server_not_passed():
    fake = FakeMT5()
    MT5Broker(MT5Config(login=123), mt5_module=fake).connect()
    assert fake.init_args[1] == {"login": 123}


def test_real_account_is_refused(monkeypatch):
    from tradingai import cli
    from tradingai.config import Config

    fake = FakeMT5(trade_mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL)
    import tradingai.brokers.mt5 as mt5mod

    monkeypatch.setattr(mt5mod, "_import_mt5", lambda: fake)
    cfg = Config(mode="live")
    with pytest.raises(SystemExit, match="ECHTGELD"):
        cli.make_broker(cfg)
    cfg.mt5.allow_real_account = True
    assert cli.make_broker(cfg) is not None
