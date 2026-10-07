import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tradingai.data.calendar import EconomicCalendar
from tradingai.data.news import ClaudeSentiment, LexiconSentiment, NewsItem, NewsMonitor, parse_feed

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>EUR/USD rises as ECB signals more hikes</title>
<description>&lt;p&gt;The euro climbs.&lt;/p&gt;</description>
<pubDate>Mon, 05 Oct 2026 11:00:00 GMT</pubDate></item>
<item><title>Gold slumps to two-week low</title><pubDate>Mon, 05 Oct 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Yen weakens after BoJ stays dovish</title><updated>2026-10-05T09:00:00Z</updated>
<summary>Bank of Japan keeps rates</summary></entry></feed>"""


def test_parse_rss_and_atom():
    items = parse_feed(RSS) + parse_feed(ATOM)
    assert [i.title for i in items][0] == "EUR/USD rises as ECB signals more hikes"
    assert items[0].summary == "The euro climbs."
    assert items[0].published == datetime(2026, 10, 5, 11, tzinfo=timezone.utc)
    assert items[2].published == datetime(2026, 10, 5, 9, tzinfo=timezone.utc)


def test_lexicon_pair_and_asset_sentiment():
    lex = LexiconSentiment()
    s = lex.analyze(parse_feed(RSS) + parse_feed(ATOM), NOW)
    assert s["EUR"] > 0 and s["USD"] < 0  # "EUR/USD rises": Euro stärker, Dollar schwächer
    assert s["XAU"] < 0  # "Gold slumps"
    assert s["JPY"] < 0  # "Yen weakens ... dovish"


def test_monitor_pair_sentiment_and_log(tmp_path):
    mon = NewsMonitor(["x"], LexiconSentiment(), log_path=tmp_path / "s.csv",
                      fetcher=lambda feeds: parse_feed(RSS))
    assert mon.update(NOW)
    assert not mon.update(NOW + timedelta(minutes=1))  # Cache: nicht ständig neu laden
    assert mon.pair_sentiment("EURUSD") > 0
    # Paar-Sentiment = Basis minus Quote (Gold fiel, aber der Dollar ebenfalls)
    assert mon.pair_sentiment("XAUUSD") == pytest.approx(mon.sentiment["XAU"] - mon.sentiment["USD"])
    assert mon.pair_sentiment("UNKNOWN") is None
    assert (tmp_path / "s.csv").read_text().startswith("time,asset,score,items")


class _FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        block = SimpleNamespace(type="text", text=json.dumps(self.payload))
        return SimpleNamespace(stop_reason=self.stop_reason, content=[block])


def _fake_client(messages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def test_claude_sentiment_parses_structured_output():
    payload = {"sentiments": [
        {"asset": "EUR", "score": 0.8, "confidence": 0.5, "reason": "hawkish ECB"},
        {"asset": "USD", "score": -2.0, "confidence": 1.0, "reason": "out of range gets clipped"},
    ]}
    msgs = _FakeMessages(payload)
    analyzer = ClaudeSentiment(client=_fake_client(msgs))
    items = parse_feed(RSS)
    out = analyzer.analyze(items, NOW)
    assert out == {"EUR": 0.4, "USD": -1.0}
    call = msgs.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"
    analyzer.analyze(items, NOW)
    assert len(msgs.calls) == 1, "gleiche Nachrichten -> kein zweiter API-Aufruf"


def test_claude_sentiment_falls_back_to_lexicon_on_refusal():
    analyzer = ClaudeSentiment(client=_fake_client(_FakeMessages({}, stop_reason="refusal")))
    out = analyzer.analyze(parse_feed(RSS), NOW)
    assert out["EUR"] > 0  # Ergebnis kommt vom Lexikon


def test_calendar_blocks_around_high_impact_events():
    data = [
        {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-05T08:30:00-04:00",
         "impact": "High"},
        {"title": "German Factory Orders", "country": "EUR", "date": "2026-10-05T06:00:00+00:00",
         "impact": "Low"},
    ]
    cal = EconomicCalendar("x", before_minutes=30, after_minutes=30, fetch=lambda: data)
    cal.update(NOW)
    nfp = datetime(2026, 10, 5, 12, 30, tzinfo=timezone.utc)
    assert cal.blocking_event(["EUR", "USD"], nfp - timedelta(minutes=20)).title.startswith("Non-Farm")
    assert cal.blocking_event(["EUR", "USD"], nfp + timedelta(minutes=29)) is not None
    assert cal.blocking_event(["EUR", "USD"], nfp + timedelta(minutes=31)) is None
    assert cal.blocking_event(["GBP", "JPY"], nfp) is None
    assert cal.blocking_event(["EUR"], datetime(2026, 10, 5, 6, tzinfo=timezone.utc)) is None  # nur "High"


def test_calendar_survives_network_errors():
    def boom():
        raise ConnectionError("offline")

    cal = EconomicCalendar("x", fetch=boom)
    cal.update(NOW)
    assert cal.events == [] and cal.blocking_event(["USD"], NOW) is None


def test_parse_feed_bytes_with_bom_and_html_detection():
    data = b"\xef\xbb\xbf\n  " + RSS.encode("utf-8")
    assert len(parse_feed(data)) == 2
    with pytest.raises(ValueError, match="Webseite"):
        parse_feed(b"<!DOCTYPE html><html><body>Access denied</body></html>")


def test_broken_feed_is_reported_only_once(caplog):
    from tradingai.data import news

    class Resp:
        def __init__(self, content, status=200):
            self.content, self.status = content, status

        def raise_for_status(self):
            if self.status >= 400:
                raise RuntimeError(f"{self.status} Client Error")

    class Session:
        def get(self, url, **kwargs):
            return Resp(b"", 404) if "dead" in url else Resp(RSS.encode("utf-8"))

    news._FEED_FAILURES.clear()
    with caplog.at_level("WARNING", logger="tradingai.data.news"):
        for _ in range(3):
            items = news.fetch_feeds(["https://dead.example/rss", "https://ok.example/rss"], session=Session())
            assert len(items) == 2
    assert sum("dead.example" in r.getMessage() for r in caplog.records) == 1
