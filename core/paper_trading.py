"""
Paper Trading Engine
======================
بيدمج كل حاجة بنيناها في تدفق واحد كامل: Scanner -> SignalEngine ->
RiskEngine -> محفظة افتراضية -> PositionManager للخروج.

**يدعم أكتر من مسار (profile) في نفس الوقت** — مثلًا "conservative"
و"scalp" — كل واحد له معايير دخول وخروج مختلفة (SignalEngine +
PositionManager منفصلين)، لكن **كلهم بيشاركوا نفس الـRiskEngine**.
ده مقصود ومهم: رأس المال واحد، الـDaily Loss Limit والـDrawdown
Scaling بيتحسبوا على الحساب كله مش لكل مسار لوحده — عشان لو الاتنين
فتحوا صفقات في نفس اليوم، الحماية من فرط التداول تفضل شغالة صح.

الاستخدام المتوقع:

    engine = PaperTradingEngine(risk_engine, scanner, profiles={
        "conservative": StrategyProfile(signal_engine_a, position_manager_a),
        "scalp": StrategyProfile(signal_engine_b, position_manager_b),
    })

    decision = engine.evaluate_candidate("scalp", token_metrics, timing_signals, entry_price, stop_loss_price)
    engine.update_position(address, current_price)   # بيعرف تلقائي المسار المناسب
    engine.portfolio_snapshot()                        # إجمالي + تقسيم لكل مسار
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.risk_engine import RiskEngine
from core.scanner import ScannerEngine, TokenMetrics
from core.strategy import (
    ExitAction,
    ExitActionType,
    PositionManager,
    PositionState,
    SignalEngine,
    TimingSignals,
)


@dataclass
class StrategyProfile:
    """
    مسار كامل: إزاي يدخل (SignalEngine) وإزاي يخرج (PositionManager).
    scanner اختياري — لو None، المسار بيستخدم الـScanner المشترك بتاع
    PaperTradingEngine. لو محدد، المسار بيقيّم أمان العملة بمنطقه
    الخاص (مفيد لما منحنى "النضج" أو "صحة الحجم" يختلف حسب طبيعة
    المسار — شوف scanner_scalp_config.yaml). الـHard Filters (سيولة
    دنيا، mint authority، honeypot...) المفروض تفضل مطابقة بين كل
    الـScanners رغم كده — الاختلاف في الـscoring curves بس، مش في
    قبول عملات أخطر.
    """
    signal_engine: SignalEngine
    position_manager: PositionManager
    scanner: Optional[ScannerEngine] = None


@dataclass
class TradeLogEntry:
    timestamp: datetime
    symbol: str
    address: str
    profile: str
    action: str  # "entry" | "partial_exit" | "full_exit" | "stop_move" | "trailing_update"
    price: float
    size_usd: float
    reason: str
    pnl_usd: Optional[float] = None


@dataclass
class VirtualPosition:
    position: PositionState
    profile: str
    entry_scanner_score: float
    entry_signal_score: float
    entry_reasons: str


@dataclass
class CandidateDecision:
    opened: bool
    profile: str
    rejection_reason: Optional[str] = None
    scanner_score: Optional[float] = None
    signal_score: Optional[float] = None
    position_size_usd: Optional[float] = None
    # لو القرار جه من evaluate_candidate_auto: كل المسارات اللي وافق
    # SignalEngine بتاعها (name -> score)، للشفافية — تعرف مين "كان
    # ممكن" يفتح الصفقة ده حتى لو اتفتحت تحت مسار تاني في الآخر
    profiles_considered: Optional[dict[str, float]] = None
    # أسباب فشل الـHard Filters لكل مسار (name -> list) — للشفافية بس
    scanner_rejection_reasons: Optional[dict[str, list]] = None


class PaperTradingEngine:
    def __init__(
        self,
        risk_engine: RiskEngine,
        scanner: ScannerEngine,
        profiles: dict[str, StrategyProfile],
        wallet_reputation_tracker=None,
    ):
        if not profiles:
            raise ValueError("لازم على الأقل profile واحد (مثلًا 'conservative')")

        self.risk_engine = risk_engine
        self.scanner = scanner
        self.profiles = profiles
        # اختياري — لو موجود، كل صفقة تتقفل بتغذي WalletReputationTracker
        # بنتيجتها عشان يتعلم سمعة المحافظ بمرور الوقت (شوف core/data/wallet_reputation.py)
        self.wallet_reputation_tracker = wallet_reputation_tracker

        self.cash_usd: float = risk_engine.current_equity_usd
        self.open_positions: dict[str, VirtualPosition] = {}
        self.trade_log: list[TradeLogEntry] = []
        self.closed_trades: list[dict] = []

    def _profile(self, name: str) -> StrategyProfile:
        if name not in self.profiles:
            raise ValueError(f"unknown_profile:{name} — المتاح: {list(self.profiles)}")
        return self.profiles[name]

    # ------------------------------------------------------------------
    # Entry: يمر على 3 أنظمة موافقة مستقلة (Risk المشترك + Scanner
    # بتاع المسار [أو المشترك لو المسار معندوش واحد خاص] + SignalEngine
    # بتاع المسار المطلوب بس)
    # ------------------------------------------------------------------
    def evaluate_candidate(
        self,
        profile_name: str,
        token_metrics: TokenMetrics,
        timing_signals: TimingSignals,
        entry_price: float,
        stop_loss_price: float,
    ) -> CandidateDecision:
        profile = self._profile(profile_name)

        if token_metrics.address in self.open_positions:
            return CandidateDecision(opened=False, profile=profile_name, rejection_reason="position_already_open")

        # 1) Risk Engine (مشترك بين كل المسارات): هل مسموح نتداول أصلًا؟
        can_trade, halt_reason = self.risk_engine.can_open_new_trade()
        if not can_trade:
            return CandidateDecision(opened=False, profile=profile_name, rejection_reason=f"risk_engine_blocked:{halt_reason}")

        # 2) Scanner بتاع المسار (أو المشترك لو المسار مالوش واحد خاص)
        scanner = profile.scanner or self.scanner
        scan_result = scanner.scan(token_metrics)
        if not scan_result.eligible_for_entry:
            reason = "scanner_hard_filter_failed" if not scan_result.passed_hard_filters else "scanner_score_too_low"
            return CandidateDecision(opened=False, profile=profile_name, rejection_reason=reason, scanner_score=scan_result.score)

        # 3) Signal Engine بتاع المسار ده تحديدًا
        signal_decision = profile.signal_engine.evaluate_entry(timing_signals)
        if not signal_decision.approved:
            return CandidateDecision(
                opened=False, profile=profile_name,
                rejection_reason=f"signal_rejected:{signal_decision.rejection_reason}",
                scanner_score=scan_result.score, signal_score=signal_decision.signal_score,
            )

        # 4) حجم الصفقة من الـRisk Engine المشترك
        size_result = self.risk_engine.calculate_position_size(entry_price, stop_loss_price)
        if not size_result.approved:
            return CandidateDecision(
                opened=False, profile=profile_name,
                rejection_reason=f"position_sizing_rejected:{size_result.reason_if_rejected}",
                scanner_score=scan_result.score, signal_score=signal_decision.signal_score,
            )

        if size_result.position_size_usd > self.cash_usd:
            return CandidateDecision(opened=False, profile=profile_name, rejection_reason="insufficient_virtual_cash")

        self._open_position(
            profile_name, token_metrics, entry_price, stop_loss_price,
            size_result.position_size_usd, scan_result.score, signal_decision.signal_score,
        )
        self.risk_engine.register_trade_opened()

        return CandidateDecision(
            opened=True, profile=profile_name,
            scanner_score=scan_result.score, signal_score=signal_decision.signal_score,
            position_size_usd=size_result.position_size_usd,
        )

    # ------------------------------------------------------------------
    # Auto-routing: العملة تتفحص ضد كل المسارات مع بعض — بالـScanner
    # الخاص بكل مسار (أو المشترك لو مالوش واحد خاص) وبعدين الـSignalEngine
    # بتاعه. أيهم وافق الاتنين، ده اللي "ممكن" يفتح الصفقة. لو أكتر من
    # مسار وافقوا، الأعلى signal_score هو اللي بيفتح فعليًا — مش قاعدة
    # مصطنعة بتحدد نوع العملة مقدمًا، لأن معندناش data تأكد قاعدة كده.
    # ------------------------------------------------------------------
    def evaluate_candidate_auto(
        self,
        token_metrics: TokenMetrics,
        timing_signals: TimingSignals,
        entry_price: float,
        stop_loss_price: float,
    ) -> CandidateDecision:
        if token_metrics.address in self.open_positions:
            return CandidateDecision(opened=False, profile="auto", rejection_reason="position_already_open")

        # 1) Risk Engine — بوابة مشتركة، بتتفحص مرة واحدة بس مش لكل مسار
        can_trade, halt_reason = self.risk_engine.can_open_new_trade()
        if not can_trade:
            return CandidateDecision(opened=False, profile="auto", rejection_reason=f"risk_engine_blocked:{halt_reason}")

        # 2) لكل مسار: الـScanner بتاعه (أو المشترك) ثم الـSignalEngine بتاعه
        approved: dict[str, float] = {}
        approved_scanner_scores: dict[str, float] = {}
        best_scanner_score = None
        best_rejection = None
        scan_reasons: dict[str, list] = {}
        for name, profile in self.profiles.items():
            scanner = profile.scanner or self.scanner
            scan_result = scanner.scan(token_metrics)
            if scan_result.rejection_reasons:
                scan_reasons[name] = list(scan_result.rejection_reasons)
            if best_scanner_score is None or (scan_result.score or 0) > best_scanner_score:
                best_scanner_score = scan_result.score

            if not scan_result.eligible_for_entry:
                if best_rejection is None:
                    reason = "scanner_hard_filter_failed" if not scan_result.passed_hard_filters else "scanner_score_too_low"
                    best_rejection = reason
                continue

            signal_decision = profile.signal_engine.evaluate_entry(timing_signals)
            if signal_decision.approved:
                approved[name] = signal_decision.signal_score
                approved_scanner_scores[name] = scan_result.score
            elif best_rejection is None:
                best_rejection = f"signal_rejected:{signal_decision.rejection_reason}"

        if not approved:
            return CandidateDecision(
                opened=False, profile="auto",
                rejection_reason=best_rejection or "no_profile_signal_approved",
                scanner_score=best_scanner_score,
                scanner_rejection_reasons=scan_reasons or None,
            )

        # أعلى signal_score هو اللي بيفوز؛ عند التعادل، أول واحد بترتيب
        # الـdict (بيرجع نفس النتيجة كل مرة لنفس المدخلات — deterministic)
        winning_profile = max(approved, key=approved.get)
        winning_score = approved[winning_profile]

        # 4) حجم الصفقة من الـRisk Engine المشترك
        size_result = self.risk_engine.calculate_position_size(entry_price, stop_loss_price)
        if not size_result.approved:
            return CandidateDecision(
                opened=False, profile="auto",
                rejection_reason=f"position_sizing_rejected:{size_result.reason_if_rejected}",
                scanner_score=approved_scanner_scores[winning_profile], signal_score=winning_score,
                profiles_considered=approved,
            )

        if size_result.position_size_usd > self.cash_usd:
            return CandidateDecision(
                opened=False, profile="auto", rejection_reason="insufficient_virtual_cash",
                profiles_considered=approved,
            )

        self._open_position(
            winning_profile, token_metrics, entry_price, stop_loss_price,
            size_result.position_size_usd, approved_scanner_scores[winning_profile], winning_score,
        )
        self.risk_engine.register_trade_opened()

        return CandidateDecision(
            opened=True, profile=winning_profile,
            scanner_score=approved_scanner_scores[winning_profile], signal_score=winning_score,
            position_size_usd=size_result.position_size_usd,
            profiles_considered=approved,
        )

    def _open_position(
        self, profile_name: str, m: TokenMetrics, entry_price: float, stop_loss_price: float,
        size_usd: float, scanner_score: float, signal_score: float,
    ) -> None:
        position = PositionState(
            symbol=m.symbol, address=m.address,
            entry_price=entry_price, initial_stop_price=stop_loss_price,
            original_size_usd=size_usd,
            top_holder_addresses=list(m.top_holder_addresses),
        )
        reasons = f"[{profile_name}] scanner_score={scanner_score:.1f}, signal_score={signal_score:.1f}"
        self.open_positions[m.address] = VirtualPosition(
            position=position, profile=profile_name, entry_scanner_score=scanner_score,
            entry_signal_score=signal_score, entry_reasons=reasons,
        )
        self.cash_usd -= size_usd
        self.trade_log.append(TradeLogEntry(
            timestamp=datetime.now(), symbol=m.symbol, address=m.address, profile=profile_name,
            action="entry", price=entry_price, size_usd=size_usd, reason=reasons,
        ))

    # ------------------------------------------------------------------
    # Exit management — كل tick بيتنادى للمراكز المفتوحة، وبيستخدم
    # PositionManager بتاع المسار اللي فتح المركز ده تحديدًا
    # ------------------------------------------------------------------
    def update_position(self, address: str, current_price: float) -> list[ExitAction]:
        vp = self.open_positions.get(address)
        if vp is None:
            return []

        position_manager = self._profile(vp.profile).position_manager
        actions = position_manager.evaluate(vp.position, current_price)
        for action in actions:
            self._apply_action(vp, action, current_price)

        if vp.position.remaining_size_pct <= 0 or any(
            a.action_type == ExitActionType.FULL_CLOSE_STOP_HIT for a in actions
        ):
            self._finalize_closed_position(vp)
            del self.open_positions[address]

        return actions

    def _apply_action(self, vp: VirtualPosition, action: ExitAction, current_price: float) -> None:
        pos = vp.position
        if action.action_type in (ExitActionType.PARTIAL_CLOSE, ExitActionType.FULL_CLOSE_STOP_HIT):
            closed_usd = pos.original_size_usd * (action.close_pct_of_original / 100)
            pnl_usd = closed_usd * ((current_price - pos.entry_price) / pos.entry_price)
            self.cash_usd += closed_usd + pnl_usd
            self.risk_engine.record_trade_result(pnl_usd)
            self.trade_log.append(TradeLogEntry(
                timestamp=datetime.now(), symbol=pos.symbol, address=pos.address, profile=vp.profile,
                action="full_exit" if action.action_type == ExitActionType.FULL_CLOSE_STOP_HIT else "partial_exit",
                price=current_price, size_usd=closed_usd, reason=action.note, pnl_usd=pnl_usd,
            ))
        elif action.action_type == ExitActionType.MOVE_STOP:
            self.trade_log.append(TradeLogEntry(
                timestamp=datetime.now(), symbol=pos.symbol, address=pos.address, profile=vp.profile,
                action="stop_move", price=action.new_stop_price or 0.0, size_usd=0.0, reason=action.note,
            ))
        elif action.action_type == ExitActionType.UPDATE_TRAILING_STOP:
            self.trade_log.append(TradeLogEntry(
                timestamp=datetime.now(), symbol=pos.symbol, address=pos.address, profile=vp.profile,
                action="trailing_update", price=action.new_stop_price or 0.0, size_usd=0.0, reason=action.note,
            ))

    def _finalize_closed_position(self, vp: VirtualPosition) -> None:
        relevant_entries = [e for e in self.trade_log if e.address == vp.position.address and e.pnl_usd is not None]
        total_pnl = sum(e.pnl_usd for e in relevant_entries)
        win = total_pnl > 0
        self.closed_trades.append({
            "symbol": vp.position.symbol,
            "address": vp.position.address,
            "profile": vp.profile,
            "entry_price": vp.position.entry_price,
            "entry_reasons": vp.entry_reasons,
            "total_pnl_usd": round(total_pnl, 2),
            "win": win,
        })

        if self.wallet_reputation_tracker is not None and vp.position.top_holder_addresses:
            self.wallet_reputation_tracker.record_trade_outcome(
                vp.position.top_holder_addresses, win=win, pnl_usd=total_pnl,
            )

    # ------------------------------------------------------------------
    # Emergency Mode — قفل فعلي لكل المراكز المفتوحة فورًا، من غير
    # المرور على منطق الـTake Profit التدريجي أو الـTrailing (الطوارئ
    # معناها "اطلع دلوقتي"، مش "اطلع بترتيب").
    #
    # الدالة دي لازم تتنادى دوريًا من الـMain Loop (لسه مبنيناهوش)، مع
    # أحدث سعر معروف لكل مركز مفتوح. لو سعر مركز معين مش متاح، بنقفله
    # بسعر الدخول (PnL = 0) ونسجل ده صراحة في السبب — أحسن من إننا
    # نسيبه مفتوح أو نخترع سعر.
    # ------------------------------------------------------------------
    def run_emergency_check(self, current_prices: dict[str, float]) -> dict:
        reason = self.risk_engine.emergency_trigger_reason()
        if reason is None:
            return {"triggered": False, "reason": None, "closed_addresses": []}

        closed_addresses: list[str] = []
        for address in list(self.open_positions.keys()):
            vp = self.open_positions[address]
            price = current_prices.get(address)
            price_missing = price is None
            if price_missing:
                price = vp.position.entry_price
            self._emergency_close_position(vp, price, reason, price_missing)
            del self.open_positions[address]
            closed_addresses.append(address)

        return {"triggered": True, "reason": reason, "closed_addresses": closed_addresses}

    def _emergency_close_position(
        self, vp: VirtualPosition, current_price: float, reason: str, price_was_missing: bool,
    ) -> None:
        pos = vp.position
        remaining_usd = pos.original_size_usd * (pos.remaining_size_pct / 100)
        pnl_usd = remaining_usd * ((current_price - pos.entry_price) / pos.entry_price)
        self.cash_usd += remaining_usd + pnl_usd
        self.risk_engine.record_trade_result(pnl_usd)

        note = f"emergency:{reason}"
        if price_was_missing:
            note += " (price_unavailable_closed_at_entry_price)"
        self.trade_log.append(TradeLogEntry(
            timestamp=datetime.now(), symbol=pos.symbol, address=pos.address, profile=vp.profile,
            action="emergency_exit", price=current_price, size_usd=remaining_usd,
            reason=note, pnl_usd=pnl_usd,
        ))
        pos.remaining_size_pct = 0.0
        self._finalize_closed_position(vp)

    # ------------------------------------------------------------------
    # Dashboard snapshot — إجمالي + تقسيم لكل مسار
    # ------------------------------------------------------------------
    def _stats_for(self, trades: list[dict]) -> dict:
        total = len(trades)
        wins = sum(1 for t in trades if t["win"])
        gross_profit = sum(t["total_pnl_usd"] for t in trades if t["total_pnl_usd"] > 0)
        gross_loss = abs(sum(t["total_pnl_usd"] for t in trades if t["total_pnl_usd"] < 0))
        return {
            "closed_trades_count": total,
            "win_rate_pct": round((wins / total) * 100, 1) if total else None,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
            "total_pnl_usd": round(sum(t["total_pnl_usd"] for t in trades), 2),
        }

    # ------------------------------------------------------------------
    # State persistence — نفس سبب RiskEngine.export_state/import_state:
    # كل تشغيلة في GitHub Actions process منفصل، لازم نكمل من غير ما
    # نفقد الصفقات المفتوحة أو الكاش أو السجل
    # ------------------------------------------------------------------
    def export_state(self) -> dict:
        return {
            "cash_usd": self.cash_usd,
            "open_positions": {
                address: {
                    "position": {
                        "symbol": vp.position.symbol, "address": vp.position.address,
                        "entry_price": vp.position.entry_price,
                        "initial_stop_price": vp.position.initial_stop_price,
                        "original_size_usd": vp.position.original_size_usd,
                        "remaining_size_pct": vp.position.remaining_size_pct,
                        "current_stop_price": vp.position.current_stop_price,
                        "highest_price_seen": vp.position.highest_price_seen,
                        "targets_hit": list(vp.position.targets_hit),
                        "trailing_active": vp.position.trailing_active,
                        "opened_at": vp.position.opened_at.isoformat(),
                        "top_holder_addresses": list(vp.position.top_holder_addresses),
                    },
                    "profile": vp.profile,
                    "entry_scanner_score": vp.entry_scanner_score,
                    "entry_signal_score": vp.entry_signal_score,
                    "entry_reasons": vp.entry_reasons,
                }
                for address, vp in self.open_positions.items()
            },
            "closed_trades": list(self.closed_trades),
            "trade_log": [
                {
                    "timestamp": e.timestamp.isoformat(), "symbol": e.symbol, "address": e.address,
                    "profile": e.profile, "action": e.action, "price": e.price,
                    "size_usd": e.size_usd, "reason": e.reason, "pnl_usd": e.pnl_usd,
                }
                for e in self.trade_log
            ],
            "risk_engine_state": self.risk_engine.export_state(),
        }

    def import_state(self, state: dict) -> None:
        self.cash_usd = state["cash_usd"]
        self.open_positions = {}
        for address, vp_dict in state["open_positions"].items():
            pos_dict = dict(vp_dict["position"])
            pos_dict["opened_at"] = datetime.fromisoformat(pos_dict["opened_at"])
            position = PositionState(**pos_dict)
            self.open_positions[address] = VirtualPosition(
                position=position, profile=vp_dict["profile"],
                entry_scanner_score=vp_dict["entry_scanner_score"],
                entry_signal_score=vp_dict["entry_signal_score"],
                entry_reasons=vp_dict["entry_reasons"],
            )
        self.closed_trades = list(state["closed_trades"])
        self.trade_log = []
        for e in state["trade_log"]:
            e = dict(e)
            e["timestamp"] = datetime.fromisoformat(e["timestamp"])
            self.trade_log.append(TradeLogEntry(**e))
        self.risk_engine.import_state(state["risk_engine_state"])

    def portfolio_snapshot(self) -> dict:
        open_positions_value = sum(
            vp.position.original_size_usd * (vp.position.remaining_size_pct / 100)
            for vp in self.open_positions.values()
        )

        by_profile = {
            name: self._stats_for([t for t in self.closed_trades if t["profile"] == name])
            for name in self.profiles
        }

        overall = self._stats_for(self.closed_trades)

        return {
            "cash_usd": round(self.cash_usd, 2),
            "open_positions_value_usd": round(open_positions_value, 2),
            "estimated_total_equity_usd": round(self.cash_usd + open_positions_value, 2),
            "open_positions_count": len(self.open_positions),
            "closed_trades_count": overall["closed_trades_count"],
            "win_rate_pct": overall["win_rate_pct"],
            "profit_factor": overall["profit_factor"],
            "by_profile": by_profile,
            "risk_engine_status": self.risk_engine.status_snapshot(),
        }
