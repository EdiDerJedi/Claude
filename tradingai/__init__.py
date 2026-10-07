"""TradingAI – selbstlernender Trading-Bot für MetaTrader 5."""

import time as _time

__version__ = "0.2.0"

# Startzeit dieses Prozesses (vor den langsamen Importen). Stopp-Anfragen, die älter
# sind, stammen aus einem früheren Lauf und werden verworfen.
PROCESS_START = _time.time()
