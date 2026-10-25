"""Backtest: نفس الاستراتيجية والمخاطر والرسوم بتاعة اللايف، بتتعاد على داتا تاريخية.

الأخبار محايدة هنا (مفيش أرشيف أخبار مجاني) — يعني الباك تيست بيقيس الجزء الفني + المخاطر بس.
الدخول بسعر فتح الشمعة اللي بعد الإشارة (بدون نظر للمستقبل).
"""
from __future__ import annotations

import copy
from typing import Dict, List

from . import features as F
from . import paper, risk
from .data import INTERVAL_MS
from .strategy import plan_trade, regime, score_symbol

STEP = INTERVAL_MS["15m"]


def prepare(client, symbols: List[str], start: int, end: int) -> Dict[str, dict]:
    """ينزل الداتا ويبني المؤشرات. warmup عشان EMA200 على 1h و EMA50 على 4h."""
    out = {}
    for s in symbols:
        try:
            ser = {
                "15m": client.klines_range(s, "15m", start - 60 * STEP, end),
                "1h": client.klines_range(s, "1h", start - 230 * INTERVAL_MS["1h"], end),
                "4h": client.klines_range(s, "4h", start - 70 * INTERVAL_MS["4h"], end),
            }
            f = F.build(ser)
            if F.enough(f):
                out[s] = f
        except Exception as e:  # noqa: BLE001
            print(f"skip {s}: {e}")
    return out


def run(feats: Dict[str, dict], cfg: dict, start: int, end: int) -> dict:
    cfg = copy.deepcopy(cfg)
    cap = float(cfg["capital_usd"])
    st = {"cash": cap, "positions": {}, "cooldowns": {}, "pause_until": 0, "consec_losses": 0, "killed": False,
          "killed_reason": None, "peak_equity": cap, "day": {"key": None, "start_equity": cap, "trades": 0}}
    btc = feats["BTCUSDT"]
    pos_idx = {s: {t: i for i, t in enumerate(f["15m"]["t"])} for s, f in feats.items()}
    trades, curve, daily = [], [], {}
    T = start
    while T <= end:
        # --- 1) الشمعة اللي اتقفلت دلوقتي (t = T - STEP) على الصفقات المفتوحة ---
        for sym, pos in list(st["positions"].items()):
            f = feats[sym]
            k = pos_idx[sym].get(T - STEP)
            if k is None:
                continue
            m = f["15m"]
            i1 = F.idx_at(f["1h"], T)
            atr_now = f["1h"]["atr"][i1] if i1 >= 0 else None
            for e in paper.process_candle(pos, m["o"][k], m["h"][k], m["l"][k], m["c"][k], m["t"][k], cfg, atr_now):
                st["cash"] += e["cash"]
                if pos["qty_open"] <= 1e-12:
                    rec = paper.close_record(pos, e["type"], T)
                    trades.append(rec)
                    risk.on_trade_closed(st, rec["pnl"], sym, e["type"] == "stop", T, cfg)
                    del st["positions"][sym]
                    break
            else:
                te = paper.time_exit(pos, T, m["c"][k], cfg) if sym in st["positions"] else None
                if te:
                    st["cash"] += te["cash"]
                    rec = paper.close_record(pos, te["type"], T)
                    trades.append(rec)
                    risk.on_trade_closed(st, rec["pnl"], sym, False, T, cfg)
                    del st["positions"][sym]
        eq = st["cash"] + sum(p["qty_open"] * p["mark"] for p in st["positions"].values())
        dk = risk.day_key(T)
        if st["day"]["key"] != dk:
            st["day"] = {"key": dk, "start_equity": eq, "trades": 0}
        st["peak_equity"] = max(st["peak_equity"], eq)
        daily[dk] = (eq / st["day"]["start_equity"] - 1) * 100
        if T % (4 * STEP) == 0:
            curve.append([T, round(eq, 2)])
        # --- 2) قرارات الدخول ---
        reg = regime(btc, T, cfg)
        if reg.get("panic"):
            st["pause_until"] = max(st["pause_until"], T + int(cfg["regime"]["panic_pause_hours"] * 3_600_000))
        ok, _ = risk.gate(st, eq, T, cfg)
        if ok and reg["ok"]:
            mult = risk.risk_multiplier(eq, st["peak_equity"], cfg)
            best = []
            for sym, f in feats.items():
                if sym in st["positions"]:
                    continue
                sc = score_symbol(sym, f, btc, T, cfg, 0.0)
                if sc["blocked"] or not sc.get("trigger_ok") or sc["score"] < cfg["entry"]["score_threshold"]:
                    continue
                best.append(sc)
            best.sort(key=lambda x: -x["score"])
            opened = 0
            for sc in best:
                g, _ = risk.gate(st, st["cash"] + sum(p["qty_open"] * p["mark"] for p in st["positions"].values()),
                                 T, cfg, sc["symbol"])
                if not g or opened >= 2:
                    break
                k = pos_idx[sc["symbol"]].get(T)
                if k is None:
                    continue
                op = feats[sc["symbol"]]["15m"]["o"][k]
                plan = plan_trade(dict(sc, price=op), cfg)
                if plan is None or "blocked" in plan:
                    continue
                eq_now = st["cash"] + sum(p["qty_open"] * p["mark"] for p in st["positions"].values())
                qty = risk.position_size(eq_now, st["cash"], op * (1 + cfg["costs"]["slippage"]), plan["stop"], cfg, mult)
                if qty * op < 11:
                    continue
                pos = paper.new_position(sc["symbol"], op, qty, plan, T, T - STEP, sc["score"], sc["reasons"], cfg)
                st["cash"] -= pos["cost_basis"] + pos["fees"]
                st["positions"][sc["symbol"]] = pos
                st["day"]["trades"] += 1
                opened += 1
        T += STEP
    # أي صفقة لسه مفتوحة آخر الفترة تتقيّم بسعر الإغلاق (من غير ما تتحسب كصفقة مقفولة)
    eq = st["cash"] + sum(p["qty_open"] * p["mark"] for p in st["positions"].values())
    return {"trades": trades, "curve": curve, "daily": daily, "final_equity": eq, "open_at_end": len(st["positions"]),
            "killed": st["killed"]}


def summarize(res: dict, cfg: dict, btc_feat: dict, start: int, end: int) -> dict:
    tr = res["trades"]
    cap = float(cfg["capital_usd"])
    wins = [t for t in tr if t["pnl"] > 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in tr if t["pnl"] <= 0)
    peak, mdd = cap, 0.0
    for _, e in res["curve"]:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak * 100)
    days = list(res["daily"].values())
    i0, i1 = F.idx_at(btc_feat["1h"], start), F.idx_at(btc_feat["1h"], end)
    bh = (btc_feat["1h"]["c"][i1] / btc_feat["1h"]["c"][i0] - 1) * 100 if i0 >= 0 and i1 > i0 else None
    by_sym: Dict[str, float] = {}
    for t in tr:
        by_sym[t["symbol"]] = by_sym.get(t["symbol"], 0.0) + t["pnl"]
    return {
        "return_pct": round((res["final_equity"] / cap - 1) * 100, 2), "final_equity": round(res["final_equity"], 2),
        "trades": len(tr), "win_rate": round(len(wins) / len(tr) * 100, 1) if tr else None,
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "avg_r": round(sum(t["r"] for t in tr) / len(tr), 3) if tr else None,
        "max_drawdown_pct": round(mdd, 2), "fees_paid": round(sum(t["fees"] for t in tr), 2),
        "days": len(days), "green_days_pct": round(sum(1 for d in days if d > 0.05) / len(days) * 100, 1) if days else None,
        "days_ge_1pct": round(sum(1 for d in days if d >= 1.0) / len(days) * 100, 1) if days else None,
        "worst_day_pct": round(min(days), 2) if days else None, "best_day_pct": round(max(days), 2) if days else None,
        "btc_buy_hold_pct": round(bh, 2) if bh is not None else None,
        "pnl_by_symbol": {k: round(v, 2) for k, v in sorted(by_sym.items(), key=lambda x: -x[1])},
        "killed": res["killed"], "open_at_end": res["open_at_end"],
    }
