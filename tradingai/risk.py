"""Risikomanagement: Positionsgröße, Stops, Tagesverlust-Limit und Not-Aus."""

import logging
import math
from datetime import datetime

from .config import RiskConfig
from .models import SymbolInfo

log = logging.getLogger(__name__)


class RiskManager:
    """Hält seinen Zustand in einem dict, das von der Engine persistiert wird."""

    def __init__(self, cfg: RiskConfig, state: dict):
        self.cfg = cfg
        self.state = state
        state.setdefault("killed", False)
        state.setdefault("kill_reason", "")

    # ----------------------------------------------------------- Kapitalstand
    def update_equity(self, equity: float, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if self.state.get("day") != day:
            self.state["day"] = day
            self.state["day_start_equity"] = equity
        peak = max(self.state.get("peak_equity", equity), equity)
        self.state["peak_equity"] = peak
        self.state["equity"] = equity

        drawdown = 1.0 - equity / peak if peak > 0 else 0.0
        if drawdown >= self.cfg.max_drawdown and not self.state["killed"]:
            self.state["killed"] = True
            self.state["kill_reason"] = f"Drawdown {drawdown:.1%} >= Limit {self.cfg.max_drawdown:.1%}"
            log.critical("NOT-AUS ausgelöst: %s", self.state["kill_reason"])

    @property
    def killed(self) -> bool:
        return bool(self.state.get("killed"))

    def reset_kill_switch(self) -> None:
        self.state["killed"] = False
        self.state["kill_reason"] = ""
        self.state.pop("peak_equity", None)

    def daily_loss(self) -> float:
        start = self.state.get("day_start_equity")
        equity = self.state.get("equity")
        if not start or equity is None:
            return 0.0
        return max(0.0, 1.0 - equity / start)

    def can_open(self, open_positions: int) -> tuple[bool, str]:
        if self.killed:
            return False, f"Not-Aus aktiv ({self.state['kill_reason']})"
        loss = self.daily_loss()
        if loss >= self.cfg.max_daily_loss:
            return False, f"Tagesverlust {loss:.1%} erreicht Limit {self.cfg.max_daily_loss:.1%}"
        if open_positions >= self.cfg.max_open_positions:
            return False, f"max. {self.cfg.max_open_positions} offene Positionen erreicht"
        return True, ""

    def spread_ok(self, spread_points: float) -> bool:
        return self.cfg.max_spread_points <= 0 or spread_points <= self.cfg.max_spread_points

    # ------------------------------------------------------- Größe und Stops
    def stop_distances(self, atr_value: float) -> tuple[float, float]:
        sl = atr_value * self.cfg.sl_atr_mult
        tp = atr_value * self.cfg.tp_atr_mult if self.cfg.tp_atr_mult > 0 else 0.0
        return sl, tp

    def position_size(self, equity: float, sl_distance: float, info: SymbolInfo) -> float:
        """Lotgröße, bei der ein Stop-Loss-Treffer genau risk_per_trade kostet."""
        if sl_distance <= 0 or info.tick_size <= 0 or info.tick_value <= 0 or equity <= 0:
            return 0.0
        risk_money = equity * self.cfg.risk_per_trade
        loss_per_lot = sl_distance / info.tick_size * info.tick_value
        volume = risk_money / loss_per_lot
        step = info.volume_step if info.volume_step > 0 else 0.01
        volume = math.floor(volume / step + 1e-9) * step
        if volume < info.volume_min:
            min_risk = info.volume_min * loss_per_lot / equity
            if self.cfg.allow_min_lot_override and min_risk <= 2 * self.cfg.risk_per_trade:
                log.info("%s: Mindestlot %.2f riskiert %.2f%% statt %.2f%%", info.name, info.volume_min,
                         min_risk * 100, self.cfg.risk_per_trade * 100)
                volume = info.volume_min
            else:
                log.info("%s: Konto zu klein für das gewünschte Risiko (Mindestlot würde %.2f%% riskieren)",
                         info.name, min_risk * 100)
                return 0.0
        volume = min(volume, info.volume_max)
        decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        return round(volume, decimals)
