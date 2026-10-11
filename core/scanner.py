"""
Scanner & Scoring Engine
========================
مسؤوليته: يقرر هل العملة دي "تستاهل" حتى إن استراتيجية الدخول تبص عليها.

بيشتغل على مرحلتين:
1. Hard Filters — فحص pass/fail. لو فشلت واحدة، العملة برة فورًا،
   وملهاش Score أصلًا (زي: liquidity ضعيفة، mint authority لسه شغالة،
   العملة مش قابلة للبيع فعليًا...)
2. Scoring — بعد ما تعدي الفلاتر، بتاخد Score من 0-100 حسب مجموعة عوامل
   موزونة. البوت مايدخلش إلا لو الـScore عدى minimum_entry_score.

ملحوظة مهمة: الكلاس ده مايعملش أي API calls أو RPC calls بنفسه —
بياخد TokenMetrics (بيانات جاهزة) ويقيّمها. جمع البيانات من on-chain/
DEX APIs هيبقى في data layer منفصل (Phase لاحقة)، عشان نقدر نـ backtest
ونـ test الـscoring logic من غير ما نحتاج اتصال حقيقي بالشبكة.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class TokenMetrics:
    """
    كل البيانات اللي الـScanner محتاجها عن عملة معينة، في لحظة معينة.
    الطبقة اللي هتجمع البيانات دي (Phase لاحقة) هي اللي هتملأ الكلاس ده
    من مصادر زي DEX APIs / on-chain RPC / indexers.
    """
    symbol: str
    address: str

    liquidity_usd: float
    market_cap_usd: float
    volume_24h_usd: float
    volume_1h_usd: float

    token_age_minutes: float

    top_holder_pct: float          # أكبر holder (غير الـpool/liquidity)
    top10_holders_pct: float
    holder_count: int
    holder_count_1h_ago: int       # عشان نحسب معدل النمو

    liquidity_locked_or_burned: bool
    mint_authority_active: bool
    freeze_authority_active: bool
    is_sellable: bool              # نتيجة simulate-sell test
    buy_tax_pct: float
    sell_tax_pct: float

    buys_last_hour: int
    sells_last_hour: int
    unique_buyers_last_hour: int

    # فلاجات جاهزة من طبقة تحليل خارجية (heuristics/ML/blacklists)
    critical_flags: list[str] = field(default_factory=list)
    warning_flags: list[str] = field(default_factory=list)

    # --- إضافات: Smart Money + Social Presence ---
    # عناوين أكبر الحاملين (غير الـLP) — لازم تتسجل وقت الدخول عشان
    # WalletReputationTracker يقدر يتعلم منها بعدين. لو فاضية، إشارة
    # الـsmart money بترجع حيادية (50) تلقائيًا.
    top_holder_addresses: list[str] = field(default_factory=list)
    # 0-100 من SocialSignalProvider — افتراضي 50 (حيادي) لو محدش حسبها
    social_presence_score: float = 50.0


@dataclass
class ScanResult:
    symbol: str
    address: str
    passed_hard_filters: bool
    rejection_reasons: list[str] = field(default_factory=list)
    score: Optional[float] = None
    score_breakdown: dict = field(default_factory=dict)
    eligible_for_entry: bool = False
    critical_flags_triggered: list[str] = field(default_factory=list)


class ScannerEngine:
    def __init__(self, config_path: str = "config/scanner_config.yaml", wallet_reputation_tracker=None):
        """
        wallet_reputation_tracker: instance اختيارية من WalletReputationTracker
        (core/data/wallet_reputation.py). لو None، إشارة الـsmart money
        بترجع حيادية (50) دايمًا — البوت بيشتغل عادي من غيرها، بس من
        غير الفايدة الإضافية.
        """
        self.config_path = config_path
        self.wallet_reputation_tracker = wallet_reputation_tracker
        self._load_config()

    def _load_config(self) -> None:
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def reload_config(self) -> None:
        self._load_config()

    # ------------------------------------------------------------------
    # Hard filters
    # ------------------------------------------------------------------
    def _run_hard_filters(self, m: TokenMetrics) -> list[str]:
        f = self.config["hard_filters"]
        reasons: list[str] = []

        if m.liquidity_usd < f["min_liquidity_usd"]:
            reasons.append(f"liquidity_below_min (${m.liquidity_usd:,.0f})")
        if m.token_age_minutes < f["min_token_age_minutes"]:
            reasons.append(f"token_too_new ({m.token_age_minutes:.0f}m)")
        if m.top_holder_pct > f["max_top_holder_pct"]:
            reasons.append(f"top_holder_too_concentrated ({m.top_holder_pct:.1f}%)")
        if m.top10_holders_pct > f["max_top10_holders_pct"]:
            reasons.append(f"top10_holders_too_concentrated ({m.top10_holders_pct:.1f}%)")
        if f["require_liquidity_locked_or_burned"] and not m.liquidity_locked_or_burned:
            reasons.append("liquidity_not_locked_or_burned")
        if f["require_sellable"] and not m.is_sellable:
            reasons.append("not_sellable_honeypot_risk")
        if f["reject_if_mint_authority_active"] and m.mint_authority_active:
            reasons.append("mint_authority_still_active")
        if f["reject_if_freeze_authority_active"] and m.freeze_authority_active:
            reasons.append("freeze_authority_still_active")
        if m.buy_tax_pct > f["max_buy_tax_pct"]:
            reasons.append(f"buy_tax_too_high ({m.buy_tax_pct:.1f}%)")
        if m.sell_tax_pct > f["max_sell_tax_pct"]:
            reasons.append(f"sell_tax_too_high ({m.sell_tax_pct:.1f}%)")

        # أي critical flag = استبعاد فوري بغض النظر عن أي حاجة تانية
        critical_defined = set(self.config["suspicious_flags"]["critical"])
        triggered_critical = [flag for flag in m.critical_flags if flag in critical_defined]
        for flag in triggered_critical:
            reasons.append(f"critical_flag:{flag}")

        return reasons

    # ------------------------------------------------------------------
    # Scoring sub-components (كل واحدة بترجع 0-100)
    # ------------------------------------------------------------------
    def _score_liquidity_depth(self, m: TokenMetrics) -> float:
        if m.market_cap_usd <= 0:
            return 0.0
        ratio = m.liquidity_usd / m.market_cap_usd
        # ratio 0.15+ يعتبر ممتاز، 0.03 وتحت ضعيف جدًا
        return max(0.0, min(100.0, (ratio / 0.15) * 100))

    def _score_volume_activity(self, m: TokenMetrics) -> float:
        if m.liquidity_usd <= 0:
            return 0.0
        vol_to_liq = m.volume_24h_usd / m.liquidity_usd
        curves = self.config.get("scoring_curves", {})
        healthy_min = curves.get("volume_healthy_min_ratio", 0.3)
        healthy_max = curves.get("volume_healthy_max_ratio", 3.0)
        # نشاط صحي: حجم تداول يومي بين healthy_min و healthy_max ضعف السيولة.
        # أعلى بكتير جدًا = مشبوه (wash trading) — بس "كتير جدًا" نسبي لطبيعة
        # المسار: عملة سكالب سريعة الحركة طبيعي يكون عندها نسبة أعلى من عملة
        # محافظة، فالحد ده قابل للتعديل لكل مسار عبر scoring_curves.
        if vol_to_liq < healthy_min:
            return (vol_to_liq / healthy_min) * 60
        elif vol_to_liq <= healthy_max:
            return 60 + ((vol_to_liq - healthy_min) / (healthy_max - healthy_min)) * 40
        else:
            return max(20.0, 100 - (vol_to_liq - healthy_max) * 5)

    def _score_holder_distribution(self, m: TokenMetrics) -> float:
        f = self.config["hard_filters"]
        top_score = max(0.0, 100 - (m.top_holder_pct / f["max_top_holder_pct"]) * 100)
        top10_score = max(0.0, 100 - (m.top10_holders_pct / f["max_top10_holders_pct"]) * 100)
        return (top_score + top10_score) / 2

    def _score_buy_sell_pressure(self, m: TokenMetrics) -> float:
        total = m.buys_last_hour + m.sells_last_hour
        if total == 0:
            return 50.0  # مفيش بيانات كفاية، نقطة حيادية
        buy_ratio = m.buys_last_hour / total
        # 0.5 = متعادل (50 نقطة)، كل ما نميل للشراء كل ما زادت، لحد سقف معقول
        return max(0.0, min(100.0, buy_ratio * 150))

    def _score_holder_growth(self, m: TokenMetrics) -> float:
        if m.holder_count_1h_ago <= 0:
            return 50.0
        growth = (m.holder_count - m.holder_count_1h_ago) / m.holder_count_1h_ago
        if growth < 0:
            return 30.0  # الهولدرز بينقصوا = إشارة سلبية
        if growth > 1.5:
            # نمو سريع جدًا جدًا (150%+ في ساعة) ممكن يكون bot activity
            return 50.0
        if growth >= 0.2:
            return 100.0  # نمو صحي وقوي (20%+ في الساعة، طبيعي لميم كوينز في أول يوم)
        return min(100.0, (growth / 0.2) * 100)

    def _score_wallet_reputation(self, m: TokenMetrics) -> float:
        """
        بتدمج مصدرين: (1) خصم من warning flags سلبية معروفة (زي كان
        موجود من الأول)، (2) Smart Money boost/penalty من
        WalletReputationTracker — **بس لو فعلًا عندنا تاريخ حقيقي عن
        واحدة من المحافظ دي**. من غير تاريخ حقيقي، النتيجة زي الوضع
        القديم بالظبط (من غير أي تأثير من مكوّن "محايد" وهمي بيسحب
        النقط لتحت من غير سبب حقيقي).
        """
        penalty = 0
        weights = self.config["suspicious_flags"]["warning_score_penalty"]
        wallet_related = {"dev_wallet_selling", "sudden_holder_concentration_change"}
        for flag in m.warning_flags:
            if flag in wallet_related and flag in weights:
                penalty += weights[flag]
        negative_signal_score = max(0.0, 100 - penalty)

        has_real_data = (
            self.wallet_reputation_tracker is not None
            and m.top_holder_addresses
            and self.wallet_reputation_tracker.has_reputation_data(m.top_holder_addresses)
        )
        if not has_real_data:
            return negative_signal_score  # زي الوضع قبل الإضافة، بالظبط

        smart_money_score = self.wallet_reputation_tracker.get_reputation_score(m.top_holder_addresses)
        return (negative_signal_score + smart_money_score) / 2

    def _score_social_presence(self, m: TokenMetrics) -> float:
        return max(0.0, min(100.0, m.social_presence_score))

    def _score_age_maturity(self, m: TokenMetrics) -> float:
        min_age = self.config["hard_filters"]["min_token_age_minutes"]
        curves = self.config.get("scoring_curves", {})
        # كم دقيقة لحد ما العملة "تنضج" بالكامل لهذا المسار — 360 (6 ساعات)
        # افتراضي معقول لمسار محافظ بيستحمل العملة فترة، لكن مسار سكالب
        # بيلاحق عملات عمرها دقايق أصلًا، فمعندوش معنى نطلب منه نفس فترة النضج
        mature_age = curves.get("age_maturity_minutes", 360)
        if m.token_age_minutes >= mature_age:
            return 100.0
        return max(0.0, min(100.0, ((m.token_age_minutes - min_age) / (mature_age - min_age)) * 100))

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def scan(self, m: TokenMetrics) -> ScanResult:
        rejection_reasons = self._run_hard_filters(m)
        critical_triggered = [r for r in rejection_reasons if r.startswith("critical_flag:")]

        if rejection_reasons:
            return ScanResult(
                symbol=m.symbol,
                address=m.address,
                passed_hard_filters=False,
                rejection_reasons=rejection_reasons,
                eligible_for_entry=False,
                critical_flags_triggered=critical_triggered,
            )

        components = {
            "liquidity_depth": self._score_liquidity_depth(m),
            "volume_activity": self._score_volume_activity(m),
            "holder_distribution": self._score_holder_distribution(m),
            "buy_sell_pressure": self._score_buy_sell_pressure(m),
            "holder_growth_rate": self._score_holder_growth(m),
            "wallet_reputation": self._score_wallet_reputation(m),
            "age_maturity": self._score_age_maturity(m),
            "social_presence": self._score_social_presence(m),
        }

        weights = self.config["scoring_weights"]
        weighted_score = sum(components[k] * weights[k] for k in components)

        # خصم إضافي من warning flags مش متعلقة بالمحافظ (اتحسبت جزئيًا فوق بالفعل)
        warning_penalties = self.config["suspicious_flags"]["warning_score_penalty"]
        general_warning_flags = {"abnormal_wash_trading_pattern", "low_unique_buyer_count"}
        for flag in m.warning_flags:
            if flag in general_warning_flags and flag in warning_penalties:
                weighted_score -= warning_penalties[flag]

        weighted_score = max(0.0, min(100.0, weighted_score))
        min_score = self.config["minimum_entry_score"]

        return ScanResult(
            symbol=m.symbol,
            address=m.address,
            passed_hard_filters=True,
            score=round(weighted_score, 2),
            score_breakdown={k: round(v, 2) for k, v in components.items()},
            eligible_for_entry=weighted_score >= min_score,
        )
