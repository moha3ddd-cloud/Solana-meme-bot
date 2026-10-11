"""
DexScreener -> Engine Data Mappers
====================================
بيحول الـJSON الخام من DexScreener لنفس الـdataclasses اللي
Scanner/Strategy Engines شغالين بيها، عشان الـEngines نفسها متتغيرش.

مهم جدًا تقرأ الملاحظات دي قبل ما تستخدم الملف:

1) TimingSignals (لـSignalEngine) — بيتبني بالكامل من DexScreener،
   لكن بتقريب مش دقيق 100%:
   - DexScreener بيديك m5 و h1 (مش m15). فـ"15m" بتاعتنا تقريب مبني
     على m5 و h1، مش رقم حقيقي. علّمناه بوضوح تحت.
   - breakout_confirmation (broke_recent_high / breakout_volume_multiple)
     مايتحسبش من snapshot واحد أصلًا — محتاج تاريخ أسعار متراكم (الـData
     Logger). هنا بنرجّع قيم افتراضية آمنة (False / 1.0) وبنسيب حساب
     القيمة الحقيقية لطبقة فوق (اللي عندها تاريخ الأسعار).

2) TokenMetrics (لـScannerEngine) — DexScreener بيديك جزء بس:
   liquidity, market cap, volume, عمر التوكن, buys/sells عدد.
   البيانات دي *مش* موجودة في DexScreener خالص ولازم تيجي من مصدر
   on-chain تاني (Solana RPC / RugCheck / Helius...):
     - top_holder_pct, top10_holders_pct, holder_count
     - liquidity_locked_or_burned
     - mint_authority_active, freeze_authority_active
     - is_sellable (honeypot test), buy_tax_pct, sell_tax_pct
     - unique_buyers_last_hour (DexScreener بيديك عدد الصفقات، مش
       عدد المحافظ الفريدة)
   عشان كده الدالة هنا بترجع dict جزئي (`build_dex_fields`) لازم يتدمج
   مع dict تاني من on-chain client (لسه هنبنيه) قبل ما نقدر نعمل
   TokenMetrics كامل.
"""

from __future__ import annotations

import time
from typing import Optional

from core.strategy import TimingSignals

# أسماء الحقول المطلوبة من مصدر on-chain عشان TokenMetrics تكتمل —
# مرجع سريع لأي حد هيبني core/data/onchain_client.py بعدين
ONCHAIN_REQUIRED_FIELDS = [
    "top_holder_pct",
    "top10_holders_pct",
    "holder_count",
    "holder_count_1h_ago",
    "liquidity_locked_or_burned",
    "mint_authority_active",
    "freeze_authority_active",
    "is_sellable",
    "buy_tax_pct",
    "sell_tax_pct",
    "unique_buyers_last_hour",
    "critical_flags",
    "warning_flags",
]


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def pair_to_timing_signals(pair: dict) -> TimingSignals:
    """
    بتحول pair JSON من DexScreener لـTimingSignals جاهزة للـSignalEngine.

    تقريب الـ15 دقيقة: بما إن DexScreener بيديك m5 و h1 بس، بنعمل
    weighted blend (70% وزن للـm5 لأنها أقرب زمنيًا لـ15 دقيقة من الساعة).
    ده تقريب مش رقم حقيقي — لو حبيت دقة أعلى، الحل الوحيد إنك تجمع
    snapshots كل دقيقة بنفسك وتحسب m15 فعلي بدل التقريب ده.
    """
    price_change = pair.get("priceChange", {}) or {}
    volume = pair.get("volume", {}) or {}
    txns = pair.get("txns", {}) or {}
    txns_5m = txns.get("m5", {}) or {}

    m5_change = _safe_float(price_change.get("m5"))
    h1_change = _safe_float(price_change.get("h1"))
    approx_15m_change = (0.7 * m5_change) + (0.3 * h1_change)

    volume_m5 = _safe_float(volume.get("m5"))
    volume_h1 = _safe_float(volume.get("h1"))
    # تقريب نفس المبدأ: مفيش رقم m15 حقيقي من المصدر
    approx_volume_15m = (volume_m5 * 3) if volume_m5 > 0 else (volume_h1 * 0.25)

    return TimingSignals(
        symbol=(pair.get("baseToken") or {}).get("symbol", "UNKNOWN"),
        address=(pair.get("baseToken") or {}).get("address", ""),
        price_change_5m_pct=m5_change,
        price_change_15m_pct=approx_15m_change,
        volume_5m_usd=volume_m5,
        volume_15m_usd=approx_volume_15m,
        volume_1h_usd=volume_h1,
        buys_last_5m=_safe_int(txns_5m.get("buys")),
        sells_last_5m=_safe_int(txns_5m.get("sells")),
        # مش قابلين للحساب من snapshot واحد — لازم تاريخ أسعار متراكم.
        # القيم دي placeholder آمن؛ الطبقة اللي فوق (بعد ما نبني الـData
        # Logger) هي اللي المفروض تحسبهم فعليًا من الأسعار المتراكمة.
        broke_recent_high=False,
        breakout_volume_multiple=1.0,
    )


def build_dex_fields(pair: dict) -> dict:
    """
    بيرجع dict فيه بس الحقول اللي DexScreener فعلًا بيوفرها من TokenMetrics.
    الـdict ده لازم يتدمج مع on-chain fields (شوف ONCHAIN_REQUIRED_FIELDS)
    قبل ما نقدر نبني TokenMetrics object كامل.

    الاستخدام المتوقع (لما نبني on-chain client بعدين):

        dex_fields = build_dex_fields(pair_json)
        onchain_fields = onchain_client.fetch(dex_fields["address"])
        metrics = TokenMetrics(**dex_fields, **onchain_fields)
    """
    base_token = pair.get("baseToken") or {}
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    txns_h1 = (pair.get("txns") or {}).get("h1", {}) or {}

    market_cap = pair.get("marketCap") or pair.get("fdv")

    pair_created_at_ms = pair.get("pairCreatedAt")
    if pair_created_at_ms:
        age_minutes = (time.time() * 1000 - pair_created_at_ms) / 60000.0
    else:
        age_minutes = 0.0  # لو المصدر مديناش تاريخ الإنشاء، نتعامل معاها كأنها جديدة (أأمن افتراض)

    return {
        "symbol": base_token.get("symbol", "UNKNOWN"),
        "address": base_token.get("address", ""),
        "liquidity_usd": _safe_float(liquidity.get("usd")),
        "market_cap_usd": _safe_float(market_cap),
        "volume_24h_usd": _safe_float(volume.get("h24")),
        "volume_1h_usd": _safe_float(volume.get("h1")),
        "token_age_minutes": max(0.0, age_minutes),
        "buys_last_hour": _safe_int(txns_h1.get("buys")),
        "sells_last_hour": _safe_int(txns_h1.get("sells")),
    }


def missing_onchain_fields_note() -> str:
    return (
        "الحقول دي لسه محتاجة on-chain client (لسه مش مبني): "
        + ", ".join(ONCHAIN_REQUIRED_FIELDS)
    )
