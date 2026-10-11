"""
Strategy & Signal Engine
========================
مسؤوليته اتنين منفصلين تمامًا:

1. Entry: هل دلوقتي لحظة كويسة ندخل فيها؟ (توقيت — منفصل عن أمان
   العملة اللي الـScanner بيحكم عليه). البوت مايدخلش إلا لو:
   - الـScanner وافق (eligible_for_entry)
   - الـRiskEngine وافق (can_open_new_trade)
   - وفوق كل ده، إشارات التوقيت هنا وافقت

2. Exit: إدارة المركز بعد الدخول — Take Profit تدريجي (يقفل جزء
   من المركز عند كل هدف ويحرك الـStop للـbreakeven)، وبعدها
   Trailing Stop للجزء المتبقي.

الكلاس ده برضو مايعملش أي تنفيذ فعلي (لا شراء ولا بيع) — بيرجع
"قرارات" (EntryDecision / list[ExitAction]) والـExecution Engine
(Phase لاحقة) هو اللي هينفذها فعليًا على السلسلة.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import yaml


# ======================================================================
# Entry
# ======================================================================

@dataclass
class TimingSignals:
    """
    بيانات لحظية عن حركة السعر/الحجم، منفصلة عن TokenMetrics بتاع الـScanner.
    دي بتتحدث كل ثانية/دقيقة، على عكس بيانات الـScanner اللي أبطأ نسبيًا.
    """
    symbol: str
    address: str

    price_change_5m_pct: float
    price_change_15m_pct: float

    volume_5m_usd: float
    volume_15m_usd: float
    volume_1h_usd: float

    buys_last_5m: int
    sells_last_5m: int

    # هل السعر كسر أعلى قمة في آخر فترة معينة، وبحجم تداول أعلى من المتوسط
    broke_recent_high: bool
    breakout_volume_multiple: float  # الحجم وقت الكسر ÷ متوسط الحجم العادي


@dataclass
class EntryDecision:
    approved: bool
    signal_score: Optional[float] = None
    signal_breakdown: dict = field(default_factory=dict)
    positive_signal_count: int = 0
    rejection_reason: Optional[str] = None


class SignalEngine:
    def __init__(self, config_path: str = "config/strategy_config.yaml"):
        self.config_path = config_path
        self._load_config()

    def _load_config(self) -> None:
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def reload_config(self) -> None:
        self._load_config()

    # ---- individual signal scores (كل واحدة 0-100) ----
    def _score_momentum(self, s: TimingSignals) -> float:
        # وزن أكبر للـ5 دقايق الأخيرة، وزن أقل لآخر 15 دقيقة
        combined = (s.price_change_5m_pct * 0.65) + (s.price_change_15m_pct * 0.35)
        # 0% تغيير = 50 نقطة (حيادي)، +20% = 100، -20% = 0
        return max(0.0, min(100.0, 50 + (combined / 20) * 50))

    def _score_volume_acceleration(self, s: TimingSignals) -> float:
        if s.volume_1h_usd <= 0:
            return 50.0
        # لو الـ15 دقيقة الأخيرة بتاخد نسبة أكبر من المتوقع من حجم الساعة = تسارع
        expected_15m_share = 15 / 60  # 25% لو الحجم موزع بالتساوي
        actual_share = s.volume_15m_usd / s.volume_1h_usd
        ratio = actual_share / expected_15m_share if expected_15m_share > 0 else 1.0
        return max(0.0, min(100.0, ratio * 50))

    def _score_buy_pressure_recent(self, s: TimingSignals) -> float:
        total = s.buys_last_5m + s.sells_last_5m
        if total == 0:
            return 50.0
        buy_ratio = s.buys_last_5m / total
        return max(0.0, min(100.0, buy_ratio * 150))

    def _score_breakout_confirmation(self, s: TimingSignals) -> float:
        if not s.broke_recent_high:
            return 30.0  # مفيش كسر = مش سلبي بالكامل، بس مش إشارة دخول قوية
        # كسر بحجم تداول أعلى من المتوسط = إشارة قوية، كسر بحجم ضعيف = مشبوه (fakeout)
        if s.breakout_volume_multiple >= 2.0:
            return 100.0
        return max(30.0, min(100.0, s.breakout_volume_multiple * 40))

    def evaluate_entry(self, s: TimingSignals) -> EntryDecision:
        cfg = self.config["entry_signals"]
        components = {
            "momentum": self._score_momentum(s),
            "volume_acceleration": self._score_volume_acceleration(s),
            "buy_pressure_recent": self._score_buy_pressure_recent(s),
            "breakout_confirmation": self._score_breakout_confirmation(s),
        }
        weights = cfg["weights"]
        weighted_score = sum(components[k] * weights[k] for k in components)

        # عدد الإشارات "الإيجابية بوضوح" (فوق 60) — مايبقاش الاعتماد على عامل واحد بس
        positive_count = sum(1 for v in components.values() if v >= 60)

        min_score = cfg["minimum_signal_score"]
        min_positive = cfg["min_positive_signals_required"]

        if weighted_score < min_score:
            return EntryDecision(
                approved=False,
                signal_score=round(weighted_score, 2),
                signal_breakdown={k: round(v, 2) for k, v in components.items()},
                positive_signal_count=positive_count,
                rejection_reason="signal_score_below_minimum",
            )

        if positive_count < min_positive:
            return EntryDecision(
                approved=False,
                signal_score=round(weighted_score, 2),
                signal_breakdown={k: round(v, 2) for k, v in components.items()},
                positive_signal_count=positive_count,
                rejection_reason="not_enough_independent_positive_signals",
            )

        return EntryDecision(
            approved=True,
            signal_score=round(weighted_score, 2),
            signal_breakdown={k: round(v, 2) for k, v in components.items()},
            positive_signal_count=positive_count,
        )


# ======================================================================
# Exit — position management (tiered take-profit + trailing stop)
# ======================================================================

class ExitActionType(str, Enum):
    PARTIAL_CLOSE = "partial_close"
    MOVE_STOP = "move_stop"
    FULL_CLOSE_STOP_HIT = "full_close_stop_hit"
    UPDATE_TRAILING_STOP = "update_trailing_stop"


@dataclass
class ExitAction:
    action_type: ExitActionType
    close_pct_of_original: float = 0.0
    new_stop_price: Optional[float] = None
    note: str = ""


@dataclass
class PositionState:
    symbol: str
    address: str
    entry_price: float
    initial_stop_price: float
    original_size_usd: float

    remaining_size_pct: float = 100.0   # % من المركز الأصلي لسه مفتوح
    current_stop_price: float = 0.0
    highest_price_seen: float = 0.0
    targets_hit: list[int] = field(default_factory=list)  # indices من take_profit_levels
    trailing_active: bool = False
    opened_at: datetime = field(default_factory=datetime.now)
    # عناوين أكبر الحاملين وقت الدخول — لـWalletReputationTracker وقت الإغلاق
    top_holder_addresses: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.current_stop_price == 0.0:
            self.current_stop_price = self.initial_stop_price
        if self.highest_price_seen == 0.0:
            self.highest_price_seen = self.entry_price

    @property
    def gain_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return ((self.highest_price_seen - self.entry_price) / self.entry_price) * 100

    def current_gain_pct(self, current_price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return ((current_price - self.entry_price) / self.entry_price) * 100


class PositionManager:
    """
    بتدير مركز واحد مفتوح، صفقة بصفقة. بتاخد إعدادات الـexit من ملف
    مستقل (config/exit_rules_conservative.yaml أو exit_rules_scalp.yaml)
    عشان نقدر نجرب أكتر من أسلوب خروج من غير ما نلمس إدارة المخاطرة
    المشتركة في risk_config.yaml (اللي واحدة بس لكل الحساب).
    """

    def __init__(self, exit_rules_config_path: str = "config/exit_rules_conservative.yaml"):
        self.exit_rules_config_path = exit_rules_config_path
        self._load_config()

    def _load_config(self) -> None:
        with open(self.exit_rules_config_path, "r", encoding="utf-8") as f:
            self.exit_rules = yaml.safe_load(f)

    def reload_config(self) -> None:
        self._load_config()

    def evaluate(self, position: PositionState, current_price: float) -> list[ExitAction]:
        actions: list[ExitAction] = []

        # حدّث أعلى سعر شايفينه (لازم يحصل قبل أي حاجة تانية)
        if current_price > position.highest_price_seen:
            position.highest_price_seen = current_price

        # 1) هل الستوب اتضرب؟
        if current_price <= position.current_stop_price:
            actions.append(ExitAction(
                action_type=ExitActionType.FULL_CLOSE_STOP_HIT,
                close_pct_of_original=position.remaining_size_pct,
                note=f"stop_hit_at_{current_price}",
            ))
            return actions  # مفيش داعي نكمل تقييم — المركز اتقفل

        gain_pct = position.current_gain_pct(current_price)

        # 2) Take profit تدريجي
        levels = self.exit_rules["take_profit_levels"]
        for idx, level in enumerate(levels):
            if idx in position.targets_hit:
                continue
            if gain_pct >= level["gain_pct"]:
                actions.append(ExitAction(
                    action_type=ExitActionType.PARTIAL_CLOSE,
                    close_pct_of_original=level["close_position_pct"],
                    note=f"target_{idx}_hit_at_{level['gain_pct']}pct_gain",
                ))
                position.targets_hit.append(idx)
                position.remaining_size_pct -= level["close_position_pct"]

                if level.get("move_stop_to_breakeven"):
                    new_stop = position.entry_price
                    if new_stop > position.current_stop_price:
                        actions.append(ExitAction(
                            action_type=ExitActionType.MOVE_STOP,
                            new_stop_price=new_stop,
                            note=f"breakeven_after_target_{idx}",
                        ))
                        position.current_stop_price = new_stop

        # 3) Trailing stop للجزء المتبقي
        activation_gain = self.exit_rules["trailing_stop_activation_pct"]
        trail_distance_pct = self.exit_rules["trailing_stop_distance_pct"]

        if gain_pct >= activation_gain:
            position.trailing_active = True

        if position.trailing_active and position.remaining_size_pct > 0:
            candidate_stop = position.highest_price_seen * (1 - trail_distance_pct / 100)
            if candidate_stop > position.current_stop_price:
                actions.append(ExitAction(
                    action_type=ExitActionType.UPDATE_TRAILING_STOP,
                    new_stop_price=candidate_stop,
                    note="trailing_stop_advanced",
                ))
                position.current_stop_price = candidate_stop

        return actions
