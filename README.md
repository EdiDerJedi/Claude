# TradingAI – selbstlernender Trading-Bot für MetaTrader 5

Ein Python-Bot, der sich mit deinem **MetaTrader 5** verbindet und selbstständig handelt.
Er **lernt laufend dazu**, holt sich **Daten aus dem Internet** (Kurse, andere Märkte,
Nachrichten, Wirtschaftskalender), **wechselt zwischen Strategien** und **passt seine
Parameter an**, wenn sich der Markt verändert.

> ⚠️ **Wichtig – bitte lesen.** Trading mit Hebelprodukten (Forex, CFDs, Gold …) ist sehr
> riskant; die meisten Privatanleger verlieren Geld. **Keine KI kann Gewinne garantieren** –
> auch eine selbstlernende nicht. Märkte sind zum größten Teil Zufall, und was in der
> Vergangenheit funktioniert hat, kann morgen versagen. Gute Ergebnisse mit synthetischen
> Testdaten sagen **nichts** über echte Märkte aus. Nutze den Bot zuerst im Backtest, dann
> wochenlang im Paper-Modus und auf einem **Demokonto**. Echtes Geld nur, das du komplett
> verlieren kannst. Der Bot ist standardmäßig so eingestellt, dass er **Echtgeldkonten
> ablehnt**.

---

## Inhalt

1. [Was der Bot macht](#was-der-bot-macht)
2. [Wie die KI lernt](#wie-die-ki-lernt)
3. [Daten aus dem Internet](#daten-aus-dem-internet)
4. [Sicherheit und Risikomanagement](#sicherheit-und-risikomanagement)
5. [Installation](#installation)
6. [Schritt für Schritt zum ersten Trade](#schritt-für-schritt-zum-ersten-trade)
   - [Dashboard (Bedienoberfläche)](#dashboard-bedienoberfläche)
7. [Befehle](#befehle)
8. [Konfiguration](#konfiguration)
9. [Dateien im state-Ordner](#dateien-im-state-ordner)
10. [Projektstruktur und Tests](#projektstruktur-und-tests)
11. [Grenzen und Ideen](#grenzen-und-ideen)

---

## Was der Bot macht

Bei jeder neuen Kerze (z. B. jede Stunde bei `H1`) läuft für jedes Symbol dieser Ablauf:

```
Kurse holen ──► 4 Strategien geben Signale ──► Lernen aus dem letzten Ergebnis
     │                                                    │
     │                         (wöchentlich) Parameter optimieren, ML-Modell neu trainieren
     ▼                                                    ▼
Konsens bilden ──► Filter: Risiko, Spread, Wirtschaftstermine, Nachrichtenlage
     │
     ▼
Order an MetaTrader 5 (immer mit Stop-Loss und Take-Profit)
```

Die vier Strategien:

| Strategie        | Idee                                                                 |
|------------------|----------------------------------------------------------------------|
| `trend`          | Trendfolge: schneller vs. langsamer gleitender Durchschnitt + ADX-Filter |
| `mean_reversion` | Gegenbewegung nach starker Übertreibung (Z-Score), nur in ruhigen Märkten |
| `breakout`       | Ausbruch aus dem Donchian-Kanal (Turtle-Prinzip)                    |
| `ml`             | Gradient-Boosting-Modell, das aus über 20 Merkmalen die Richtung schätzt |

Derselbe Code läuft im **Backtest**, im **Paper-Trading** (Simulation mit Live-Kursen) und
**live** mit MetaTrader 5. Was im Backtest funktioniert, funktioniert also technisch genauso live.

## Wie die KI lernt

Der Bot lernt auf drei Ebenen:

**1. Laufend: Welche Strategie passt gerade? (Online-Lernen)**
Jede Strategie wird für jedes Symbol „im Schatten“ mitgehandelt – auch wenn ihr Signal nicht
ausgeführt wird. Nach jeder Kerze wird verbucht, wie viel jede Strategie verdient oder verloren
hätte (inkl. Spread-Kosten). Daraus entsteht ein Qualitäts-Score (ähnlich einer Sharpe-Ratio).
Ältere Ergebnisse verlieren mit jeder Kerze an Gewicht (`learning.decay`), so dass sich der Bot
an neue Marktphasen anpasst: Läuft der Markt im Trend, bekommt `trend` mehr Gewicht; wird er
seitwärts, übernimmt `mean_reversion`. Laufen alle Strategien schlecht, gewinnt die eingebaute
Option **„cash“** – der Bot bleibt dann einfach draußen.

**2. Wöchentlich: Bessere Parameter finden (Walk-Forward-Optimierung)**
Für jede Regel-Strategie werden viele Parameterkombinationen auf älteren Daten getestet. Die
besten werden dann auf den neuesten Daten geprüft, die sie vorher **nie gesehen** haben.
Neue Parameter werden **nur übernommen, wenn sie dort klar besser** sind. Das verhindert, dass
sich der Bot an Zufallsmuster „überanpasst“.

**3. Wöchentlich: ML-Modell neu trainieren**
Das Modell lernt aus Kursmustern, Volatilität, Uhrzeit **und Daten anderer Märkte** (VIX,
S&P 500, Dollar-Index, US-Zinsen). Es wird zeitlich korrekt validiert (älteste 75 % zum Lernen,
neueste 25 % zum Prüfen, mit Sicherheitslücke dazwischen). Ein neues Modell wird nur aktiv,
wenn es in der Prüfung **besser als Zufall** war. Außerdem werden ML-Signale auf Daten, die das
Modell schon im Training gesehen hat, nicht bewertet – so wird es nicht schöngerechnet.

Alles Gelernte wird im Ordner `state/` gespeichert und nach einem Neustart weiterverwendet.

## Daten aus dem Internet

| Quelle | Wofür | Kosten |
|--------|-------|--------|
| **Yahoo Finance** | Kurse für Paper-Trading/Backtests, Kontextmärkte (VIX, S&P 500, DXY, US10Y) als Lernmerkmale | kostenlos |
| **RSS-Nachrichten** (FXStreet, CNBC, Fed, EZB – frei konfigurierbar) | Nachrichten-Stimmung je Währung; Einstiege gegen eine klar negative Nachrichtenlage werden blockiert | kostenlos |
| **Wirtschaftskalender** (ForexFactory-Feed) | keine neuen Trades 30 min vor/nach wichtigen Terminen (z. B. US-Arbeitsmarktdaten, Zinsentscheide) | kostenlos |
| **Claude (optional)** | bessere Analyse der Nachrichten statt Wortliste (`internet.news_analyzer: claude`) | API-Kosten, braucht `ANTHROPIC_API_KEY` |

Jede Nachrichten-Messung wird in `state/sentiment_log.csv` gespeichert – so entsteht mit der
Zeit eine eigene Historie, die später als zusätzliches Lernmerkmal genutzt werden kann.
Fällt eine Internetquelle aus, läuft der Bot ohne sie weiter.

## Sicherheit und Risikomanagement

- **Echtgeld-Sperre:** Im Live-Modus prüft der Bot den Kontotyp. Bei einem Echtgeldkonto
  bricht er ab, solange `mt5.allow_real_account: false` ist.
- **Jeder Trade hat einen Stop-Loss** (Standard: 2 × ATR) und einen Take-Profit (3 × ATR).
- **Positionsgröße nach Risiko:** Ein Stop-Loss-Treffer kostet `risk_per_trade` (Standard 1 %)
  des Kapitals. Ist das Konto zu klein für das Mindestlot, wird nicht gehandelt.
- **Tagesverlust-Limit:** Ab 3 % Tagesverlust keine neuen Trades bis zum nächsten Tag.
- **Not-Aus:** Fällt das Kapital 15 % unter den Höchststand, schließt der Bot alle seine
  Positionen und handelt nicht mehr, bis du ihn mit `reset-killswitch` freigibst.
- **Max. offene Positionen**, optionaler **Spread-Filter** und **Trailing-Stop**.
- **Manuelle Trades werden nie angefasst** – der Bot erkennt seine Orders an der `magic`-Nummer.

## Installation

### Windows (nötig für den Live-Handel)

1. **Python 3.13 (64-Bit) installieren:** <https://www.python.org/downloads/windows/> →
   bei einer **3.13.x**-Version „Windows installer (64-bit)“ wählen. Im ersten Fenster des
   Installers den Haken bei **„Add python.exe to PATH“** setzen.
   Nimm **nicht Python 3.15 oder neuer** (erscheint gerade): Dafür gibt es das Paket
   `MetaTrader5` noch nicht. 3.12, 3.13 oder 3.14 funktionieren.
2. **Code herunterladen:** auf GitHub im Repository auf den grünen Button **„Code“ →
   „Download ZIP“** klicken. Die ZIP-Datei mit Rechtsklick → **„Alle extrahieren …“** → als
   Ziel nur `C:\` eintragen → „Extrahieren“. Die ZIP enthält einen Unterordner (z. B.
   `Claude-claude-metatrader-trading-ai-w9jf0h`) – benenne ihn in **`TradingAI`** um, dann liegt
   `install.bat` direkt in `C:\TradingAI`. Lege den Ordner **nicht** auf den Desktop oder unter
   „Dokumente“, wenn diese mit OneDrive synchronisiert werden – die Synchronisierung stört.
   (Wer Git hat: `git clone https://github.com/EdiDerJedi/Claude.git C:\TradingAI`.)
3. **Doppelklick auf `install.bat`.** Das Skript sucht ein passendes Python (fehlt es, wird
   Python 3.13 automatisch über winget installiert), legt die Umgebung `.venv` an,
   installiert alle Pakete (bei Störungen automatisch bis zu drei Versuche) und erstellt `config.yaml`.
   Meldet Windows „Der Computer wurde durch Windows geschützt“: **„Weitere Informationen“ →
   „Trotzdem ausführen“**.

Befehle gibst du in der Eingabeaufforderung im Projektordner ein: Im Explorer in den Ordner
gehen, oben in die Adresszeile `cmd` tippen und Enter drücken. Statt `python -m tradingai`
schreibst du unter Windows einfach **`tradingai.bat`**, z. B.:

```bat
tradingai.bat backtest --source synthetic --bars 3000
```

In PowerShell (z. B. über Rechtsklick → „Im Terminal öffnen“) schreibst du `.\tradingai.bat …`.

Den Bot startest du per **Doppelklick auf `bot_starten.bat`** (Fenster offen lassen). Es kann
immer nur **ein** Bot pro `state`-Ordner laufen; ein zweiter Start wird mit einer Meldung
abgelehnt. Verliert MetaTrader 5 die Verbindung oder ist beim Start noch nicht bereit,
versucht der Bot es automatisch erneut.

### macOS / Linux (Backtest und Paper-Trading)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

### MetaTrader 5 vorbereiten (nur für `mode: live`)

1. MetaTrader 5 von deinem Broker installieren und ein **Demokonto** eröffnen
   (Menü Datei → „Konto eröffnen“ oder im Navigator Rechtsklick auf „Konten“; viele Broker
   bieten das Demokonto auch beim ersten Start an).
2. Einloggen. In **Extras → Optionen → Expert Advisors** den Haken bei
   **„Algorithmischen Handel erlauben“** setzen und oben in der Symbolleiste
   **„Algo Trading“** einschalten (grün).
   Im selben Fenster darf der Haken bei „… über externe Python-API deaktivieren“ **nicht**
   gesetzt sein. Ist eine dieser Einstellungen falsch, zeigt das Dashboard einen Hinweis.
3. Die Symbole, die du handeln willst, im Fenster **Marktübersicht** einblenden. Trage die
   Namen **genau so** in `config.yaml` ein (manche Broker nutzen Endungen wie `EURUSD.m`).
4. Das Terminal muss laufen, solange der Bot handelt. Login-Daten in `config.yaml` sind
   optional – ohne sie nutzt der Bot das im Terminal eingeloggte Konto.

## Schritt für Schritt zum ersten Trade

> Unter Windows in allen folgenden Befehlen `python -m tradingai` durch `tradingai.bat`
> ersetzen.

**1. Backtest mit synthetischen Daten** (funktioniert offline, prüft die Installation):

```bash
python -m tradingai backtest --source synthetic --bars 3000
```

**2. Backtest mit echten Kursen von Yahoo** (ca. 2 Jahre H1-Daten):

```bash
python -m tradingai backtest --source yahoo
python -m tradingai backtest --source yahoo --no-learn   # zum Vergleich: ohne Lernen
```

Ergebnisse (Kapitalkurve, Trades, Gelerntes) landen im Ordner `reports/`. Ein Backtest mit
Lernen dauert je Symbol und Jahr H1-Daten einige Minuten.

**3. Paper-Trading** (`mode: paper`) – simuliert mit Live-Kursen, ohne Broker:

```bash
python -m tradingai train   # einmal vorab lernen (optional, passiert sonst beim Start)
python -m tradingai run
```

**4. Demokonto über MetaTrader 5** – in `config.yaml` `mode: live` setzen, dann (beim Wechsel
von Paper zu MT5 lernt die KI automatisch neu mit den Kursen deines Brokers):

```bash
python -m tradingai run
```

Lass den Bot **mehrere Wochen** auf dem Demokonto laufen und vergleiche mit dem Backtest.

**5. Echtgeld** – nur nach erfolgreichen Demo-Wochen: `mt5.allow_real_account: true` und
`risk.risk_per_trade` eher auf `0.005` (0,5 %) senken.

### Dashboard (Bedienoberfläche)

Statt im schwarzen Fenster lässt sich alles im Browser bedienen: **Doppelklick auf
„TradingAI Dashboard“ auf dem Desktop** (legt `install.bat` an) oder auf
`dashboard_starten.bat`. Es öffnet sich <http://127.0.0.1:8765> mit:

- **Bot-Status** (läuft / wartet auf MetaTrader 5 / gestoppt / unerwartet beendet …) und den
  Knöpfen **Bot starten** und **Bot stoppen** – der Bot läuft dann unsichtbar im Hintergrund.
  Auch der Browser-Tab zeigt den Zustand (grüner Punkt = läuft, ⚠ = Problem).
- **Equity, Kontostand, Gewinn/Verlust des Tages, Rückgang vom Höchststand** (mit Not-Aus-Grenze),
  Trefferquote und Gesamtergebnis. Läuft der Bot nicht, werden die Werte gedimmt und als
  „letzter Stand“ gekennzeichnet.
- **Kapitalverlauf** als Diagramm für 24 Stunden, 7 oder 30 Tage oder alles (mit Tabellenansicht)
- **Offene Positionen** mit Stop-Loss, Take-Profit und aktuellem Gewinn/Verlust
- **Letzte Trades** mit Grund (Stop-Loss, Take-Profit, Signal, manuell geschlossen)
- **Strategien & Lernen** je Symbol: Konsens, Gewicht jeder Strategie, Status des KI-Modells,
  gelernte Einstellungen
- **Wichtige Wirtschaftstermine**, **Nachrichtenlage** und das **Protokoll**
- **Hinweise** bei Not-Aus (mit Knopf zum Zurücksetzen), Fehlern in `config.yaml`, nicht
  erreichbarem MetaTrader 5, ausgeschaltetem „Algo Trading“, Echtgeld-Konto, Tageslimit,
  Wochenende und wenn der Bot unerwartet beendet wurde (mit Fehlermeldung)
- **Einstellungen:** `config.yaml` im Editor öffnen, Bot automatisch mit dem Dashboard starten
  und Dashboard mit Windows starten (siehe unten)

Das Dashboard und der Bot sind getrennt: **Browser oder Dashboard schließen stoppt den Bot
nicht.** Gestoppt wird er mit „Bot stoppen“ (oder `Strg+C`, wenn er in einem Fenster läuft). Hängt
ein vom Dashboard gestarteter Bot, erscheint zusätzlich „Zwangsweise beenden“. Das Dashboard ist
nur auf diesem PC erreichbar. Läuft der Bot schon über `bot_starten.bat`, zeigt das Dashboard ihn
ebenfalls an und kann ihn stoppen.

### Dauerhaft laufen lassen

Für 24/5-Betrieb eignet sich ein Windows-VPS. Auf dem eigenen PC:

- **Energieoptionen:** Windows-Einstellungen → System → „Energie“ bzw. „Energie & Akku“
  (je nach Version auch „Stromversorgung & Akku“) → Bildschirm-/Energiespar-Timeouts →
  Energiesparmodus bei Netzbetrieb auf **Nie**. Tipp: in der Windows-Suche „Energiesparmodus“ eintippen.
- **Automatisch starten (am einfachsten):** im Dashboard auf **„Einstellungen“** klicken und beide
  Haken setzen – „Bot automatisch starten, wenn das Dashboard startet“ und „Dashboard beim
  Windows-Start im Hintergrund starten“. Nach jedem Neustart läuft der Bot dann von selbst weiter.
  MetaTrader 5 wird beim Verbinden in der Regel automatisch mitgestartet; falls nicht, MT5 ebenfalls
  in den Autostart legen (`Win+R` → `shell:startup` → Verknüpfung zu MT5 hineinlegen). Startet der
  Bot vor MT5, wartet er, bis MT5 bereit ist.
- **Alternative über die Aufgabenplanung:** „Einfache Aufgabe erstellen“ → Trigger „Beim
  Anmelden“ → Aktion „Programm starten“ → Programm `C:\TradingAI\.venv\Scripts\pythonw.exe`,
  Argumente `-m tradingai run`, Starten in `C:\TradingAI`. Auf der letzten Seite den Haken bei
  „Beim Klicken auf „Fertig stellen“ die Eigenschaften für diese Aufgabe öffnen“ setzen, dann:
  - Reiter **„Einstellungen“**: Haken bei **„Aufgabe beenden, falls Ausführung länger als:
    3 Tage“ entfernen** – sonst beendet Windows den Bot nach drei Tagen.
  - Reiter **„Bedingungen“**: Haken bei „Aufgabe nur starten, falls Computer im
    Netzbetrieb ausgeführt wird“ entfernen (wichtig bei Laptops).
- **Beenden:** im Dashboard **„Bot stoppen“**. Läuft der Bot in einem Fenster (`bot_starten.bat`),
  geht auch `Strg+C`; die Frage „Batchvorgang abbrechen (J/N)?“ dann mit **`N`** beantworten – das
  Fenster bleibt offen und zeigt „Der Bot wurde beendet.“ Offene Positionen behalten immer ihren
  Stop-Loss und Take-Profit.
- **Kontowechsel:** Wechselst du das Konto (z. B. von Paper auf das MT5-Demokonto oder auf ein neues
  Demokonto), erkennt der Bot das, legt die alte Statistik im Ordner `state\archiv` ab und beginnt
  Kapitalverlauf, Höchststand und Trade-Statistik neu.

## Befehle

| Befehl | Beschreibung |
|--------|--------------|
| `python -m tradingai backtest [--source yahoo\|csv\|synthetic] [--symbols EURUSD XAUUSD] [--bars N] [--no-learn] [--no-ml]` | historischer Test inkl. Lernen |
| `python -m tradingai train` | sofort Parameter optimieren und ML-Modell trainieren |
| `python -m tradingai run [--once]` | Bot starten (`mode` aus `config.yaml`) |
| `python -m tradingai status` | Gewichte, gelernte Parameter, Modellqualität, Risiko-Status |
| `python -m tradingai news` | aktuelle Nachrichtenstimmung und wichtige Termine |
| `python -m tradingai reset-killswitch` | Not-Aus nach Prüfung zurücksetzen |
| `python -m tradingai dashboard [--port N] [--no-browser]` | Dashboard im Browser öffnen |

Mit `-c andere.yaml` lässt sich eine andere Konfiguration verwenden.

## Konfiguration

Alle Einstellungen stehen kommentiert in [`config.example.yaml`](config.example.yaml).
Die wichtigsten:

| Einstellung | Standard | Bedeutung |
|-------------|----------|-----------|
| `mode` | `paper` | `paper` = Simulation, `live` = MetaTrader 5 |
| `symbols`, `timeframe` | `[EURUSD, GBPUSD, XAUUSD]`, `H1` | was und auf welcher Zeitebene gehandelt wird |
| `risk.risk_per_trade` | `0.01` | Risiko je Trade (1 %) |
| `risk.max_drawdown` | `0.15` | Not-Aus-Schwelle |
| `learning.decay` | `0.99` | Gedächtnis des Online-Lernens (kleiner = passt sich schneller an) |
| `learning.entry_threshold` | `0.35` | wie einig sich die Strategien für einen Einstieg sein müssen |
| `learning.optimize_every_hours` / `retrain_every_hours` | `168` | wie oft neu gelernt wird |
| `internet.news_analyzer` | `lexicon` | `claude` für KI-Nachrichtenanalyse |
| `paper.data_source` | `yahoo` | Kursquelle für Paper/Backtest |

Eigene Kursdaten (z. B. aus MT5 exportiert: Ansicht → Symbole (Strg+U) → Reiter Balken →
Balken exportieren) als `data/EURUSD_H1.csv` ablegen und `--source csv` verwenden.

**Claude für die Nachrichtenanalyse:** einen API-Key unter
<https://console.anthropic.com> erstellen, als Umgebungsvariable `ANTHROPIC_API_KEY` setzen und
`internet.news_analyzer: claude` einstellen. Der Bot fragt nur, wenn neue Schlagzeilen
erschienen sind; bei Fehlern fällt er automatisch auf die Wortliste zurück.

## Fehlerbehebung (Windows und MetaTrader 5)

| Meldung | Lösung |
|---------|--------|
| `install.bat`: „Could not install packages due to an OSError … No such file or directory … pip-unpack …“ | Meist blockiert ein Virenscanner die Installation. `install.bat` einfach erneut starten (bereits geladene Pakete werden wiederverwendet, es versucht es automatisch bis zu dreimal). Hilft das nicht: Virenscanner kurz pausieren oder den Projektordner als Ausnahme eintragen. |
| `install.bat`: „Es wurde kein passendes Python gefunden“ | Python 3.13 (64-Bit) installieren, Haken „Add python.exe to PATH“ setzen, `install.bat` erneut starten. |
| `MT5-Initialisierung fehlgeschlagen: (-10005, 'IPC timeout')` oder `(-10003, …)` | MetaTrader 5 von Hand starten und einloggen, dann den Bot starten. MT5 und Bot nicht unterschiedlich „als Administrator“ ausführen. Bei mehreren MT5-Installationen `mt5.path` setzen. |
| `MT5-Initialisierung fehlgeschlagen: (-6, …)` | Login, Passwort oder Server in `config.yaml` falsch – oder `login: 0` setzen und das im Terminal eingeloggte Konto verwenden. |
| `retcode=10027` | Der Button **„Algo Trading“** in MT5 ist aus – einschalten (grün). Ist er grün: unter Extras → Optionen → Expert Advisors den Haken bei „… über externe Python-API deaktivieren“ (engl. „Disable automated trading via external Python API“) entfernen. |
| `retcode=10018` | Markt geschlossen (Wochenende/Feiertag) – normal. |
| `retcode=10019` | Nicht genug freie Margin – Risiko senken oder Konto aufladen (Demo). |
| `retcode=10016` | Stops ungültig – meist bei sehr hohem Spread; später erneut. |
| `Symbol … nicht verfügbar` | Symbolnamen genau wie in der MT5-Marktübersicht schreiben (z. B. `EURUSD.m`). |
| `Der Bot läuft bereits mit dem Ordner …` | Es läuft schon ein Bot – evtl. unsichtbar im Hintergrund (über das Dashboard, den Autostart oder die Aufgabenplanung gestartet). Im Dashboard „Bot stoppen“ klicken bzw. ein offenes Bot-Fenster mit `Strg+C` beenden. `train` und `reset-killswitch` gehen nur, wenn der Bot gestoppt ist. |
| `MetaTrader 5 ist noch nicht bereit … neuer Versuch in 30 Sekunden` | MT5 starten und einloggen – der Bot verbindet sich dann von selbst. |
| Im Fenster tut sich nichts, Titel beginnt mit „Auswählen“ | Es wurde Text markiert; `Esc` drücken. (Der Bot schaltet diese Windows-Funktion beim Start normalerweise selbst ab.) |
| `trades.csv ist gerade geöffnet (Excel?)` | Excel schließen – die Einträge werden automatisch nachgetragen. |
| Doppelklick auf das Dashboard tut nichts | In `tradingai_fehler.log` bzw. `state\dashboard.log` im Projektordner nachsehen. Meist ein Tippfehler in `config.yaml` – das Dashboard zeigt ihn beim nächsten Start als Hinweis an. |
| Installation scheitert immer wieder | Den Ordner `.venv` löschen und `install.bat` erneut starten. |
| `zu wenig Historie zum Lernen` | In MT5 einen Chart des Symbols im passenden Zeitrahmen öffnen und mit `Pos1` zurückscrollen; der Bot versucht es bei jeder neuen Kerze erneut. |
| Fehler beim Laden von Yahoo-Daten | Internetverbindung/Firewall prüfen und später erneut versuchen. |
| `config.yaml`-Fehler mit Windows-Pfaden | Pfade und Passwörter in **einfache** Anführungszeichen setzen: `path: 'C:\Program Files\MetaTrader 5\terminal64.exe'`. |

## Dateien im state-Ordner

| Datei | Inhalt |
|-------|--------|
| `state.json` | gelernte Strategie-Gewichte, Parameter, Modellqualität, Risiko-Status, letzte Entscheidungen |
| `models/*.joblib` | trainierte ML-Modelle je Symbol |
| `trades.csv` | Journal aller geschlossenen Trades |
| `sentiment_log.csv` | Historie der Nachrichtenstimmung |
| `paper_account.json` | Kontostand und Positionen im Paper-Modus |
| `tradingai.log` | Protokoll (rotierend) |
| `heartbeat.json`, `status.json` | Lebenszeichen und Momentaufnahme des Bots für das Dashboard |
| `equity_history.csv` | Verlauf von Kontostand und Equity (alle 5 Minuten) |
| `dashboard.log`, `bot_stderr.log` | Protokoll des Dashboards bzw. Startfehler des Bots |

Ordner löschen = Bot fängt mit dem Lernen von vorne an.

## Projektstruktur und Tests

```
tradingai/
  engine.py          Hauptlogik: Signale → Lernen → Entscheidung → Risiko → Order
  learning/          selector.py (Online-Lernen), optimizer.py (Walk-Forward), trainer.py (ML)
  strategies/        trend, mean_reversion, breakout, ml
  brokers/           mt5.py (MetaTrader 5), sim.py (Backtest- und Paper-Broker)
  data/              market_data.py (Yahoo/CSV/synthetisch), news.py, calendar.py, symbols.py
  risk.py            Positionsgröße, Limits, Not-Aus
  features.py        Merkmale für das ML-Modell (ohne Blick in die Zukunft)
  backtest.py, cli.py, config.py, state.py
tests/               automatische Tests (MT5 wird dort nachgebildet)
```

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Die Tests prüfen u. a., dass Indikatoren, Merkmale und Strategien **keine zukünftigen Daten**
verwenden, dass Stop-Loss, Kurslücken und Spread korrekt simuliert werden, dass der Not-Aus
greift und dass die MT5-Orders korrekt aufgebaut werden (gegen ein nachgebautes MT5-Modul).

## Grenzen und Ideen

- Die Simulation nutzt einen festen Spread und Näherungswerte für Tick-Werte; echte
  Ausführung (Slippage, Swap, Kommission) ist schlechter. Live kommen die echten Werte vom Broker.
- Yahoo-Kurse weichen leicht von den Kursen deines Brokers ab und sind verzögert –
  für Paper-Trading reicht das, für den Livehandel nutzt der Bot die MT5-Kurse.
- Nur MetaTrader **5** wird unterstützt (für MT4 gibt es keine offizielle Python-Schnittstelle).
- Erweiterungsideen: weitere Strategien in `tradingai/strategies/` (einfach von `Strategy`
  erben und in `RULE_STRATEGIES` eintragen), Nachrichten-Historie als ML-Merkmal,
  Benachrichtigungen per Telegram.
