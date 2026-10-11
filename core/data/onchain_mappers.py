"""
RugCheck -> TokenMetrics Mapper + Final Merge
================================================
بيحول تقرير RugCheck للحقول اللي كانت ناقصة من DexScreener، وبيوفر
دالة merge نهائية تجمع الاتنين في TokenMetrics كامل جاهز للـScanner.

فيه 3 حقول لسه مش متغطية حتى بعد RugCheck، لأنها محتاجة simulate-sell
حقيقي على السلسلة (مش أي API خارجي هيوفرها بدقة):
  - is_sellable          -> افتراضي False (فشل آمن، مش تفاؤل)
  - buy_tax_pct           -> افتراضي 0.0 (هيتراجع لو الـsimulate كشف tax)
  - sell_tax_pct          -> افتراضي 0.0
دول هيتحدثوا فعليًا في الـExecution Engine (Phase 4) وقت ما نعمل
quote/simulate فعلي عبر Jupiter قبل التنفيذ. لحد ما ده يتبني، أي
عملة هتتقفل بالـis_sellable=False الافتراضي ما لم RugCheck يقول
صراحة إنها sellable — يعني فشل آمن (نرفض) مش فشل متفائل (نقبل).

كمان holder_count_1h_ago و unique_buyers_last_hour مفيش مصدر حالي
ليهم غير تاريخ متراكم من الـData Logger بتاعنا — بنحطهم بقيمة تقريبية
محافظة (holder_count نفسه، وbuys_last_hour) موثقة تحت.
"""

from __future__ import annotations

from typing import Optional

from core.scanner import TokenMetrics

# كلمات مفتاحية في اسم الـrisk بتاع RugCheck اللي بتعتبر "خطر جسيم"
# (بتتحول لـcritical_flags اللي الـScanner بيستبعد عليها فورًا)
_CRITICAL_RISK_KEYWORDS = {
    "honeypot": "honeypot_detected",
    "cannot sell": "honeypot_detected",
    "unable to sell": "honeypot_detected",
    "transfer fee": "transfer_restrictions_detected",
    "high transfer tax": "transfer_restrictions_detected",
    "lp removed": "liquidity_removal_in_progress",
    "liquidity removed": "liquidity_removal_in_progress",
    "known scam": "known_rug_wallet_involved",
    "blacklisted deployer": "known_rug_wallet_involved",
}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_critical_flags(risks: list[dict]) -> list[str]:
    flags = []
    for risk in risks or []:
        name = str(risk.get("name", "")).lower()
        for keyword, flag in _CRITICAL_RISK_KEYWORDS.items():
            if keyword in name and flag not in flags:
                flags.append(flag)
    return flags


def _compute_holder_concentration(top_holders: list[dict], lp_addresses: set[str]) -> tuple[float, float]:
    """
    بيرجع (top_holder_pct, top10_holders_pct) بعد استبعاد محافظ الـLP/pool
    نفسها من الحساب — عشان مانعاقبش عملة عادي على إن السيولة بتاعتها
    "محفظة كبيرة".
    """
    real_holders = [
        h for h in (top_holders or [])
        if h.get("address") not in lp_addresses
    ]
    if not real_holders:
        return 0.0, 0.0

    sorted_holders = sorted(real_holders, key=lambda h: _safe_float(h.get("pct")), reverse=True)
    top_holder_pct = _safe_float(sorted_holders[0].get("pct")) if sorted_holders else 0.0
    top10_pct = sum(_safe_float(h.get("pct")) for h in sorted_holders[:10])
    return top_holder_pct, top10_pct


def build_onchain_fields(report: dict) -> dict:
    """
    بيرجع dict فيه الحقول اللي RugCheck قادر يوفرها فعليًا.
    شوف docstring الملف للحقول اللي لسه محتاجة simulate-sell حقيقي.
    """
    markets = report.get("markets") or []
    lp_addresses = set()
    lp_locked = False
    for market in markets:
        lp = market.get("lp") or {}
        lp_addresses.update(lp.get("holders_addresses", []))
        # RugCheck بيديك نسبة مقفولة/محروقة للـLP — لو 100% (أو قريب منها) نعتبرها آمنة
        locked_pct = _safe_float(lp.get("lpLockedPct"))
        if locked_pct >= 95.0:
            lp_locked = True

    top_holders = report.get("topHolders") or []
    top_holder_pct, top10_pct = _compute_holder_concentration(top_holders, lp_addresses)

    # عناوين أكبر الحاملين الحقيقيين (بعد استبعاد الـLP) — لـWalletReputationTracker.
    # بنسجل أكبر 10 بس (زي top10_holders_pct) عشان مانضخمش قائمة العناوين لكل عملة
    real_holders_sorted = sorted(
        [h for h in top_holders if h.get("address") not in lp_addresses],
        key=lambda h: _safe_float(h.get("pct")), reverse=True,
    )
    top_holder_addresses = [h.get("address") for h in real_holders_sorted[:10] if h.get("address")]

    total_holders = report.get("totalHolders")
    holder_count = int(total_holders) if total_holders is not None else len(top_holders)

    mint_authority = report.get("mintAuthority")
    freeze_authority = report.get("freezeAuthority")

    risks = report.get("risks") or []
    critical_flags = _extract_critical_flags(risks)
    # أي risk تاني معروف من RugCheck بنسجله كـwarning عام (informational —
    # مش كل الأسماء دي متطابقة مع مفاتيح warning_score_penalty في
    # scanner_config.yaml، فمعظمها هيبقى للتسجيل بس من غير خصم نقط،
    # إلا لو ضفتها هناك بنفس الاسم).
    warning_flags = [str(r.get("name", "")).lower().replace(" ", "_") for r in risks
                      if r.get("name") and r.get("level", "").lower() != "danger"]

    return {
        "top_holder_pct": round(top_holder_pct, 2),
        "top10_holders_pct": round(top10_pct, 2),
        "holder_count": holder_count,
        "top_holder_addresses": top_holder_addresses,
        # مفيش تاريخ متراكم لسه (محتاج الـData Logger يجمع بيانات بمرور
        # الوقت) — بنفترض مفيش نمو بدل ما نخترع رقم، وده تقدير محافظ
        "holder_count_1h_ago": holder_count,
        "liquidity_locked_or_burned": lp_locked,
        "mint_authority_active": mint_authority is not None and mint_authority != "",
        "freeze_authority_active": freeze_authority is not None and freeze_authority != "",
        # فشل آمن — انتظر simulate-sell حقيقي من الـExecution Engine
        "is_sellable": "honeypot_detected" not in critical_flags,
        "buy_tax_pct": 0.0,
        "sell_tax_pct": 0.0,
        "critical_flags": critical_flags,
        "warning_flags": warning_flags,
    }


def merge_into_token_metrics(
    dex_fields: dict,
    onchain_fields: dict,
    buys_last_hour: Optional[int] = None,
    social_presence_score: float = 50.0,
) -> TokenMetrics:
    """
    بيدمج الحقول من DexScreener (build_dex_fields) و RugCheck
    (build_onchain_fields) في TokenMetrics كامل جاهز للـScanner.

    unique_buyers_last_hour: RugCheck وDexScreener مايوفروش عدد محافظ
    فريدة (بس عدد صفقات). بنستخدم buys_last_hour من DexScreener كتقدير
    تقريبي (حد أعلى، مش دقيق) لحد ما نبني تتبع محافظ حقيقي.

    social_presence_score: من core/data/social_signals.py
    (SocialSignalProvider.get_social_score) — احسبها من نفس الـpair
    JSON بتاع DexScreener قبل ما تنادي الدالة دي. افتراضي 50 (حيادي).
    """
    unique_buyers_estimate = buys_last_hour if buys_last_hour is not None else dex_fields.get("buys_last_hour", 0)

    merged = {**dex_fields, **onchain_fields}
    merged["unique_buyers_last_hour"] = unique_buyers_estimate
    merged["social_presence_score"] = social_presence_score

    return TokenMetrics(**merged)
