"""Scoring strategy (long-only spot).

الفكرة: نظام نقاط من 100 بيجمع 5 مجموعات إشارات مستقلة، والدخول بس لو المجموع ≥ الحد.
  1) اتجاه (30)   2) زخم (25)   3) محفز دخول على 15m (30)   4) قوة نسبية قصاد BTC (10)   5) أخبار (5)
بالإضافة لفلاتر صارمة (regime / تقلب / مطاردة السعر / تكلفة مقابل مخاطرة).
"""
from __future__ import annotations

from typing import Dict, Optional

from . import indicators as I
from .features import idx_at


def regime(btc: dict, t: int, cfg: dict) -> dict:
    """حالة السوق من BTC. ok=False → ما فيش دخول جديد."""
    rc = cfg["regime"]
    i1, i4 = idx_at(btc["1h"], t), idx_at(btc["4h"], t)
    if i1 < 60 or i4 < 51:
        return {"ok": False, "reason": "بيانات BTC مش كافية", "btc_trend": None}
    c1, e50 = btc["1h"]["c"][i1], btc["1h"]["ema50"][i1]
    c4, e4 = btc["4h"]["c"][i4], btc["4h"]["ema50"][i4]
    if not I.ok(e50, e4):
        return {"ok": False, "reason": "مؤشرات BTC لسه بتتحسب", "btc_trend": None}
    drop = (btc["1h"]["c"][i1] / btc["1h"]["c"][i1 - 4] - 1) * 100 if i1 >= 4 else 0.0
    trend_ok = (c1 > e50) or (c4 > e4)
    info = {"btc_trend": "up" if trend_ok else "down", "btc_4h_change_pct": round(drop, 2)}
    if drop <= -rc["panic_drop_pct"]:
        return {"ok": False, "reason": f"BTC هبط {drop:.1f}% في 4 ساعات (هلع)", "panic": True, **info}
    if rc["require_btc_trend"] and not trend_ok:
        return {"ok": False, "reason": "BTC تحت المتوسطات — السوق هابط، الكاش أحسن", **info}
    return {"ok": True, "reason": "السوق مناسب", **info}


def score_symbol(sym: str, f: dict, btc: dict, t: int, cfg: dict,
                 news_score: float = 0.0) -> dict:
    """يرجع dict فيه score وتفاصيل. لو فلتر صارم اتكسر: score=0 و`blocked` فيه السبب."""
    ec = cfg["entry"]
    out = {"symbol": sym, "score": 0, "parts": {}, "reasons": [], "blocked": None}
    i1, i4, i15 = idx_at(f["1h"], t), idx_at(f["4h"], t), idx_at(f["15m"], t)
    if i1 < 205 or i4 < 51 or i15 < 40:
        out["blocked"] = "بيانات ناقصة"
        return out
    h1, h4, m = f["1h"], f["4h"], f["15m"]
    close1, e20, e50, e200 = h1["c"][i1], h1["ema20"][i1], h1["ema50"][i1], h1["ema200"][i1]
    rsi1, rsi1p, hist, histp = h1["rsi"][i1], h1["rsi"][i1 - 3], h1["hist"][i1], h1["hist"][i1 - 1]
    atr1 = h1["atr"][i1]
    c4, e4 = h4["c"][i4], h4["ema50"][i4]
    p15, ema15, rsi15 = m["c"][i15], m["ema20"][i15], m["rsi"][i15]
    if not I.ok(close1, e20, e50, e200, rsi1, rsi1p, hist, histp, atr1, c4, e4, ema15, rsi15):
        out["blocked"] = "مؤشرات ناقصة"
        return out
    price = p15
    atr_pct = atr1 / price * 100
    out.update(price=price, atr1=atr1, atr_pct=round(atr_pct, 2))

    # ---- فلاتر صارمة ----
    if atr_pct < ec["min_atr_pct"]:
        out["blocked"] = f"تقلب ضعيف ({atr_pct:.2f}%) — التكاليف هتاكل الربح"
        return out
    if atr_pct > ec["max_atr_pct"]:
        out["blocked"] = f"تقلب عالي جداً ({atr_pct:.1f}%)"
        return out
    if (price - e20) > ec["max_extension_atr"] * atr1:
        out["blocked"] = "السعر مبتعد جداً عن المتوسط (مطاردة)"
        return out
    if close1 < e200:
        out["blocked"] = "تحت EMA200 على 1h (اتجاه كبير هابط)"
        return out

    parts, why = {}, []
    # ---- 1) اتجاه (30) ----
    tr = 0
    if c4 > e4:
        tr += 10; why.append("فوق EMA50 على 4h")
    if e20 > e50 > e200:
        tr += 15; why.append("EMA20>EMA50>EMA200 على 1h")
    elif e20 > e50:
        tr += 8; why.append("EMA20>EMA50 على 1h")
    if e50 > h1["ema50"][i1 - 6]:
        tr += 5; why.append("EMA50 صاعد")
    parts["trend"] = tr
    # ---- 2) زخم (25) ----
    mo = 0
    if 52 <= rsi1 <= 70:
        mo += 10; why.append(f"RSI 1h={rsi1:.0f} (زخم صحي)")
        if rsi1 > rsi1p:
            mo += 5
    elif rsi1 > 75:
        mo -= 10; why.append(f"RSI 1h={rsi1:.0f} (تشبع شراء)")
    if hist > 0 and hist > histp:
        mo += 10; why.append("MACD إيجابي ومتزايد")
    parts["momentum"] = max(mo, 0)
    # ---- 3) محفز دخول 15m (30) ----
    trg = 0
    hh = m["hh16"][i15]
    breakout = hh is not None and p15 > hh
    lo_rsi = min(x for x in m["rsi"][i15 - 8:i15 + 1] if x is not None) if any(
        x is not None for x in m["rsi"][i15 - 8:i15 + 1]) else None
    pullback = lo_rsi is not None and lo_rsi < 42 and rsi15 > 45 and p15 > ema15
    if breakout:
        trg += 15; why.append("كسر أعلى قمة آخر 4 ساعات")
    elif pullback:
        trg += 15; why.append("ارتداد بعد تصحيح (RSI15 رجع فوق 45)")
    vs = m["vsma"][i15]
    if vs:
        vr = m["v"][i15] / vs if vs > 0 else 0
        if vr >= 1.5:
            trg += 10; why.append(f"فوليوم {vr:.1f}x المتوسط")
        elif vr >= 1.2:
            trg += 5
    if p15 > ema15:
        trg += 5
    parts["trigger"] = trg
    # ---- 4) قوة نسبية قصاد BTC (10) ----
    rs = 0
    if sym == "BTCUSDT":
        rs = 5
    else:
        b1 = idx_at(btc["1h"], t)
        if b1 >= 24 and i1 >= 24:
            sr = close1 / h1["c"][i1 - 24] - 1
            br = btc["1h"]["c"][b1] / btc["1h"]["c"][b1 - 24] - 1
            if sr > br and sr > 0:
                rs = 10; why.append(f"أقوى من BTC (24h: {sr*100:+.1f}% مقابل {br*100:+.1f}%)")
    parts["rel_strength"] = rs
    # ---- 5) أخبار (5) ----
    nb = cfg["news"]["positive_bonus"] if news_score >= 1.0 else 0
    if nb:
        why.append("أخبار إيجابية")
    parts["news"] = nb

    out["parts"] = parts
    out["score"] = sum(parts.values())
    out["reasons"] = why
    out["trigger_ok"] = trg >= 15   # لازم محفز حقيقي (كسر أو ارتداد)، مش مجرد اتجاه
    return out


def plan_trade(sc: dict, cfg: dict) -> Optional[dict]:
    """يحسب الستوب والأهداف، ويفحص نسبة التكلفة للمخاطرة."""
    ex, ec, cs = cfg["exit"], cfg["entry"], cfg["costs"]
    price, atr1 = sc["price"], sc["atr1"]
    stop_dist = max(ex["stop_atr_mult"] * atr1, price * ex["min_stop_pct"] / 100)
    stop = price - stop_dist
    r_pct = stop_dist / price
    round_trip = 2 * (cs["fee_rate"] + cs["slippage"])
    if round_trip / r_pct > ec["max_cost_to_risk"]:
        return {"blocked": f"التكلفة {round_trip/r_pct*100:.0f}% من المخاطرة (أكتر من الحد)"}
    return {
        "entry": price, "stop": stop, "tp1": price + ex["tp1_r"] * stop_dist,
        "stop_dist": stop_dist, "atr1": atr1, "cost_to_risk": round_trip / r_pct,
    }
