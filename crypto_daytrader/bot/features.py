"""بناء المؤشرات لكل timeframe مرة واحدة، والـlive والـbacktest بيستخدموا نفس الكود."""
from __future__ import annotations

from bisect import bisect_right
from typing import Dict

from . import indicators as I

MIN_1H = 210   # عشان EMA200
MIN_4H = 55
MIN_15M = 60


def build(series: Dict[str, Dict[str, list]]) -> dict:
    """series = {'15m': S, '1h': S, '4h': S} → features dict."""
    f = {}
    s15, s1, s4 = series["15m"], series["1h"], series["4h"]
    f["15m"] = {
        "ct": s15["ct"], "t": s15["t"], "o": s15["o"], "h": s15["h"], "l": s15["l"], "c": s15["c"], "v": s15["v"],
        "ema20": I.ema(s15["c"], 20), "rsi": I.rsi(s15["c"], 14),
        "vsma": I.sma(s15["v"], 20), "hh16": I.rolling_max_prev(s15["h"], 16),
    }
    f["1h"] = {
        "ct": s1["ct"], "t": s1["t"], "c": s1["c"],
        "ema20": I.ema(s1["c"], 20), "ema50": I.ema(s1["c"], 50), "ema200": I.ema(s1["c"], 200),
        "rsi": I.rsi(s1["c"], 14), "hist": I.macd_hist(s1["c"]), "atr": I.atr(s1["h"], s1["l"], s1["c"], 14),
    }
    f["4h"] = {"ct": s4["ct"], "t": s4["t"], "c": s4["c"], "ema50": I.ema(s4["c"], 50)}
    return f


def idx_at(feat_tf: dict, t: int) -> int:
    """index آخر شمعة *مقفولة* وقت t (close_time < t). -1 لو مفيش."""
    return bisect_right(feat_tf["ct"], t - 1) - 1


def enough(f: dict) -> bool:
    return (len(f["1h"]["c"]) >= MIN_1H and len(f["4h"]["c"]) >= MIN_4H and len(f["15m"]["c"]) >= MIN_15M)
