"""
Risk Engine
===========
ده القلب بتاع النظام كله. مفيهوش أي منطق دخول/خروج بتاع الصفقات —
مسؤوليته الوحيدة: تحديد هل مسموح نفتح صفقة النهاردة ولا لأ، وبأد إيه.

المبدأ الأساسي: "إدارة المخاطر أهم من الاستراتيجية نفسها" —
يعني حتى لو الـScanner/Strategy لقت فرصة ممتازة، الـRiskEngine
هو اللي عنده الكلمة الأخيرة.

كل حاجة هنا قابلة للقراءة من config/risk_config.yaml من غير
تعديل الكود.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional

import yaml

logger = logging.getLogger("risk_engine")


class TradingHaltReason(str, Enum):
    NONE = "none"
    DAILY_LOSS_LIMIT = "daily_loss_limit_hit"
    MAX_TRADES_REACHED = "max_trades_per_day_reached"
    EMERGENCY_DRAWDOWN = "emergency_drawdown_triggered"
    CONSECUTIVE_ERRORS = "consecutive_execution_errors"
    KILL_SWITCH = "manual_kill_switch"


@dataclass
class PositionSizeResult:
    """ناتج حساب حجم الصفقة — لو approved=False يبقى ممنوع الدخول."""
    approved: bool
    position_size_usd: float = 0.0
    risk_amount_usd: float = 0.0
    effective_risk_pct: float = 0.0
    drawdown_multiplier_applied: float = 1.0
    reason_if_rejected: Optional[str] = None


@dataclass
class DailyState:
    """حالة اليوم الحالي — بترجع لصفر كل يوم تداول جديد."""
    trading_day: date
    starting_equity_usd: float
    realized_pnl_usd: float = 0.0
    trades_opened: int = 0
    consecutive_errors: int = 0
    halted: bool = False
    halt_reason: TradingHaltReason = TradingHaltReason.NONE

    @property
    def daily_pnl_pct(self) -> float:
        if self.starting_equity_usd == 0:
            return 0.0
        return (self.realized_pnl_usd / self.starting_equity_usd) * 100


class RiskEngine:
    def __init__(self, config_path: str = "config/risk_config.yaml"):
        self.config_path = config_path
        self._load_config()

        self.peak_equity_usd: float = self.config["account"]["starting_capital_usd"]
        self.current_equity_usd: float = self.peak_equity_usd
        self.kill_switch_active: bool = False

        self.daily_state = self._new_daily_state(self.current_equity_usd)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    def _load_config(self) -> None:
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def reload_config(self) -> None:
        """يسمح بتغيير الإعدادات من الـDashboard وهو البوت شغال."""
        self._load_config()
        logger.info("Risk config reloaded")

    # ------------------------------------------------------------------
    # Daily lifecycle
    # ------------------------------------------------------------------
    def _new_daily_state(self, equity_usd: float) -> DailyState:
        return DailyState(trading_day=date.today(), starting_equity_usd=equity_usd)

    def start_new_trading_day_if_needed(self) -> None:
        if self.daily_state.trading_day != date.today():
            logger.info(
                "New trading day. Yesterday PnL: $%.2f (%.2f%%)",
                self.daily_state.realized_pnl_usd,
                self.daily_state.daily_pnl_pct,
            )
            self.daily_state = self._new_daily_state(self.current_equity_usd)

    # ------------------------------------------------------------------
    # Equity / drawdown tracking
    # ------------------------------------------------------------------
    def update_equity(self, new_equity_usd: float) -> None:
        self.current_equity_usd = new_equity_usd
        if new_equity_usd > self.peak_equity_usd:
            self.peak_equity_usd = new_equity_usd

    @property
    def current_drawdown_pct(self) -> float:
        if self.peak_equity_usd == 0:
            return 0.0
        return ((self.peak_equity_usd - self.current_equity_usd) / self.peak_equity_usd) * 100

    def _drawdown_risk_multiplier(self) -> float:
        """كل ما الـdrawdown يكبر، المضاعِف يقل. مفيش 'تعويض بصفقات أكبر'."""
        dd = self.current_drawdown_pct
        multiplier = 1.0
        for tier in self.config["drawdown_scaling"]["tiers"]:
            if dd >= tier["drawdown_pct"]:
                multiplier = tier["risk_multiplier"]
        return multiplier

    # ------------------------------------------------------------------
    # Trade result recording
    # ------------------------------------------------------------------
    def record_trade_result(self, pnl_usd: float) -> None:
        self.start_new_trading_day_if_needed()
        self.daily_state.realized_pnl_usd += pnl_usd
        self.update_equity(self.current_equity_usd + pnl_usd)
        self._evaluate_halts()

    def record_execution_error(self) -> None:
        self.daily_state.consecutive_errors += 1
        self._evaluate_halts()

    def record_execution_success(self) -> None:
        self.daily_state.consecutive_errors = 0

    def activate_kill_switch(self) -> None:
        self.kill_switch_active = True
        logger.warning("KILL SWITCH ACTIVATED — no new trades until manually cleared")

    def deactivate_kill_switch(self) -> None:
        self.kill_switch_active = False

    # ------------------------------------------------------------------
    # Halt evaluation
    # ------------------------------------------------------------------
    def _evaluate_halts(self) -> None:
        cfg = self.config
        daily_limit_pct = cfg["daily_limits"]["daily_loss_limit_pct"]
        max_trades = cfg["daily_limits"]["max_trades_per_day"]
        max_errors = cfg["emergency"]["max_consecutive_errors"]
        emergency_dd = cfg["emergency"]["emergency_drawdown_pct"]

        if self.daily_state.daily_pnl_pct <= -abs(daily_limit_pct):
            self._halt(TradingHaltReason.DAILY_LOSS_LIMIT)
        elif self.daily_state.trades_opened >= max_trades:
            self._halt(TradingHaltReason.MAX_TRADES_REACHED)
        elif self.daily_state.consecutive_errors >= max_errors:
            self._halt(TradingHaltReason.CONSECUTIVE_ERRORS)
        elif self.current_drawdown_pct >= emergency_dd:
            self._halt(TradingHaltReason.EMERGENCY_DRAWDOWN)

    def _halt(self, reason: TradingHaltReason) -> None:
        if not self.daily_state.halted:
            logger.warning("Trading halted: %s", reason.value)
        self.daily_state.halted = True
        self.daily_state.halt_reason = reason

    # ------------------------------------------------------------------
    # Main entry point: can we trade, and how big?
    # ------------------------------------------------------------------
    def can_open_new_trade(self) -> tuple[bool, str]:
        self.start_new_trading_day_if_needed()

        if self.kill_switch_active:
            return False, TradingHaltReason.KILL_SWITCH.value
        if self.daily_state.halted:
            return False, self.daily_state.halt_reason.value
        return True, TradingHaltReason.NONE.value

    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss_price: float,
    ) -> PositionSizeResult:
        """
        بتحسب حجم الصفقة بناءً على مكان الـStop Loss —
        مش رقم دولار ثابت. لو الستوب اتضرب، الخسارة تساوي
        بالظبط نسبة المخاطرة المسموح بيها (بعد drawdown scaling).
        """
        can_trade, reason = self.can_open_new_trade()
        if not can_trade:
            return PositionSizeResult(approved=False, reason_if_rejected=reason)

        if stop_loss_price >= entry_price or stop_loss_price <= 0:
            return PositionSizeResult(
                approved=False,
                reason_if_rejected="invalid_stop_loss_placement",
            )

        base_risk_pct = self.config["position_sizing"]["risk_per_trade_pct"]
        min_risk_pct = self.config["position_sizing"]["min_risk_per_trade_pct"]

        dd_multiplier = self._drawdown_risk_multiplier()
        effective_risk_pct = max(base_risk_pct * dd_multiplier, 0.0)

        if dd_multiplier == 0.0:
            return PositionSizeResult(
                approved=False,
                reason_if_rejected="emergency_drawdown_zero_risk",
                drawdown_multiplier_applied=dd_multiplier,
            )

        # مايقلش عن الحد الأدنى المسموح بيه (وإلا يبقى مفيش داعي نتداول أصلًا)
        effective_risk_pct = max(effective_risk_pct, min_risk_pct)

        risk_amount_usd = self.current_equity_usd * (effective_risk_pct / 100)
        stop_distance_pct = (entry_price - stop_loss_price) / entry_price
        # الستوب مش بيتنفّذ بالظبط عند سعره: البوت بيفحص السعر كل فترة،
        # وعملات الميم بتعدّي الستوب بفجوة (gap). بنفترض فجوة إضافية
        # (stop_gap_buffer_pct) ونحسب الحجم على أساس مسافة ستوب أوسع،
        # فالخسارة الفعلية المتوقعة لو الستوب اتجاوز بالفجوة دي = نسبة المخاطرة.
        gap_buffer = self.config["position_sizing"].get("stop_gap_buffer_pct", 0.0) / 100
        position_size_usd = risk_amount_usd / (stop_distance_pct + gap_buffer)

        # حماية: الصفقة متتجاوزش رأس المال المتاح
        position_size_usd = min(position_size_usd, self.current_equity_usd)

        return PositionSizeResult(
            approved=True,
            position_size_usd=round(position_size_usd, 2),
            risk_amount_usd=round(risk_amount_usd, 2),
            effective_risk_pct=round(effective_risk_pct, 3),
            drawdown_multiplier_applied=dd_multiplier,
        )

    def register_trade_opened(self) -> None:
        self.daily_state.trades_opened += 1

    # ------------------------------------------------------------------
    # Emergency Mode — مختلف عن can_open_new_trade() اللي بيمنع صفقات
    # جديدة بس. الدالة دي بتحدد هل الظرف وصل لدرجة "لازم نقفل المراكز
    # المفتوحة فعليًا فورًا"، مش بس نوقف نفتح جديد.
    # ------------------------------------------------------------------
    def emergency_trigger_reason(self) -> Optional[str]:
        """
        بيرجع سبب التفعيل لو فيه ظرف طارئ، وإلا None. الحالات التلاتة:
        1. Kill Switch اتفعّل يدويًا — نفترض إن قصد المستخدم "وقف كل
           حاجة فورًا"، مش بس منع صفقات جديدة.
        2. الـDrawdown وصل لحد الطوارئ (نفس emergency_drawdown_pct اللي
           already بيصفّر المخاطرة في position sizing).
        3. أخطاء تنفيذ متتالية وصلت للحد الأقصى — مؤشر على مشكلة في
           RPC/DEX نفسها، مش في السوق.
        """
        if self.kill_switch_active:
            return "kill_switch_active"
        if self.current_drawdown_pct >= self.config["emergency"]["emergency_drawdown_pct"]:
            return "emergency_drawdown_exceeded"
        if self.daily_state.consecutive_errors >= self.config["emergency"]["max_consecutive_errors"]:
            return "consecutive_execution_errors"
        return None

    # ------------------------------------------------------------------
    # State persistence — عشان البوت يقدر يكمل من اللي وقف عنده بين
    # تشغيلة والتانية (مثلًا GitHub Actions: كل دورة عملية منفصلة،
    # مش process واحد شغال باستمرار زي VPS)
    # ------------------------------------------------------------------
    def export_state(self) -> dict:
        return {
            "peak_equity_usd": self.peak_equity_usd,
            "current_equity_usd": self.current_equity_usd,
            "kill_switch_active": self.kill_switch_active,
            "daily_state": {
                "trading_day": self.daily_state.trading_day.isoformat(),
                "starting_equity_usd": self.daily_state.starting_equity_usd,
                "realized_pnl_usd": self.daily_state.realized_pnl_usd,
                "trades_opened": self.daily_state.trades_opened,
                "consecutive_errors": self.daily_state.consecutive_errors,
                "halted": self.daily_state.halted,
                "halt_reason": self.daily_state.halt_reason.value,
            },
        }

    def import_state(self, state: dict) -> None:
        self.peak_equity_usd = state["peak_equity_usd"]
        self.current_equity_usd = state["current_equity_usd"]
        self.kill_switch_active = state["kill_switch_active"]
        ds = state["daily_state"]
        self.daily_state = DailyState(
            trading_day=date.fromisoformat(ds["trading_day"]),
            starting_equity_usd=ds["starting_equity_usd"],
            realized_pnl_usd=ds["realized_pnl_usd"],
            trades_opened=ds["trades_opened"],
            consecutive_errors=ds["consecutive_errors"],
            halted=ds["halted"],
            halt_reason=TradingHaltReason(ds["halt_reason"]),
        )
        # نتأكد إن يوم تداول جديد (لو عدّى منتصف الليل بين التشغيلتين) بيتصفّر صح
        self.start_new_trading_day_if_needed()

    # ------------------------------------------------------------------
    # Dashboard snapshot
    # ------------------------------------------------------------------
    def status_snapshot(self) -> dict:
        return {
            "current_equity_usd": round(self.current_equity_usd, 2),
            "peak_equity_usd": round(self.peak_equity_usd, 2),
            "current_drawdown_pct": round(self.current_drawdown_pct, 2),
            "drawdown_risk_multiplier": self._drawdown_risk_multiplier(),
            "daily_pnl_usd": round(self.daily_state.realized_pnl_usd, 2),
            "daily_pnl_pct": round(self.daily_state.daily_pnl_pct, 2),
            "trades_opened_today": self.daily_state.trades_opened,
            "trading_halted": self.daily_state.halted,
            "halt_reason": self.daily_state.halt_reason.value,
            "kill_switch_active": self.kill_switch_active,
        }
