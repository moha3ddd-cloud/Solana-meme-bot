"""
GeckoTerminal -> Engine Mappers
==================================
GeckoTerminal بيرجع ردوده بشكل JSON:API (data/attributes/relationships/
included) — مختلف تمامًا عن شكل DexScreener البسيط. الملف ده بيحول
الشكل ده لنفس الـdataclasses اللي باقي المشروع عارفها، عشان
الـScanner/Strategy/DataLogger متلزمش تعرف حاجة عن GeckoTerminal
نفسه.
"""

from __future__ import annotations

from datetime import datetime

from core.data.data_logger import OHLCCandle


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def ohlcv_response_to_candles(response: dict, pool_address: str) -> list[OHLCCandle]:
    """
    بيحول رد /ohlcv/{timeframe} لقائمة OHLCCandle — نفس الكلاس اللي
    DataLogger بيستخدمه، عشان أي كود backtesting بعدين يقدر يتعامل
    مع التاريخ الحقيقي من GeckoTerminal ومع الـsnapshots اللي جمعناها
    بنفسنا بنفس الشكل بالظبط.

    شكل الرد المتوقع: {"data": {"attributes": {"ohlcv_list": [[ts, o, h, l, c, v], ...]}}}
    الـts بالثواني (unix). بنرجعها مرتبة من الأقدم للأحدث عشان تتوافق
    مع export_ohlc في DataLogger.
    """
    try:
        ohlcv_list = response["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        return []

    candles = []
    for entry in ohlcv_list:
        if len(entry) < 6:
            continue
        ts, o, h, l, c, v = entry[:6]
        candles.append(OHLCCandle(
            address=pool_address,
            interval_start_ts=int(ts),
            open=_safe_float(o),
            high=_safe_float(h),
            low=_safe_float(l),
            close=_safe_float(c),
            volume_usd=_safe_float(v),
            snapshot_count=1,  # شمعة حقيقية من المصدر، مش مجمّعة من snapshots زي DataLogger
        ))

    candles.sort(key=lambda c: c.interval_start_ts)
    return candles


def _build_token_index(included: list[dict]) -> dict:
    """بيبني dict من token_id (زي 'solana_<address>') لبياناته — من قسم included."""
    index = {}
    for item in included or []:
        if item.get("type") == "token":
            index[item["id"]] = item.get("attributes", {})
    return index


def pools_response_to_candidates(response: dict) -> list[dict]:
    """
    بيحول رد trending_pools/new_pools لقائمة dicts بسيطة — كل عنصر
    فيه العناوين والبيانات الأساسية، جاهزة تتدمج مع DiscoveryLayer
    زي ما build_dex_fields بتعمل مع DexScreener.

    كل عنصر في الناتج:
    {address, symbol, liquidity_usd, volume_5m_usd, pool_created_at_ms}
    """
    data = response.get("data") or []
    included = response.get("included") or []
    token_index = _build_token_index(included)

    results = []
    for pool in data:
        attrs = pool.get("attributes", {})
        relationships = pool.get("relationships", {})

        base_token_rel = (relationships.get("base_token") or {}).get("data") or {}
        base_token_id = base_token_rel.get("id", "")
        base_token_attrs = token_index.get(base_token_id, {})

        # عنوان التوكن الفعلي — الـid بيجي بشكل "solana_<address>"
        address = base_token_attrs.get("address") or base_token_id.replace("solana_", "")
        symbol = base_token_attrs.get("symbol", "UNKNOWN")

        liquidity_usd = _safe_float(attrs.get("reserve_in_usd"))
        volume_5m_usd = _safe_float((attrs.get("volume_usd") or {}).get("m5"))

        pool_created_at_ms = None
        created_at_str = attrs.get("pool_created_at")
        if created_at_str:
            try:
                dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                pool_created_at_ms = int(dt.timestamp() * 1000)
            except ValueError:
                pool_created_at_ms = None

        results.append({
            "address": address,
            "symbol": symbol,
            "pool_address": attrs.get("address", ""),
            "liquidity_usd": liquidity_usd,
            "volume_5m_usd": volume_5m_usd,
            "pool_created_at_ms": pool_created_at_ms,
        })

    return results
