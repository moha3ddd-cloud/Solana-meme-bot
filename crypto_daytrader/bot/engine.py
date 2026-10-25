"""دورة التداول الحية (paper). بتتنادى كل 15 دقيقة من GitHub Actions، والحالة بتتحفظ في JSON."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from . import data as D
from . import features as F
from . import news as N
from . import paper, risk
from .strategy import plan_trade, regime, score_symbol

HOUR = 3_600_000
CURVE_MAX = 3000
TRADES_MAX = 400


def new_state(cfg: dict, now: int) -> dict:
    cap = float(cfg["capital_usd"])
    return {
        "version": 1, "created_ts": now, "initial_capital": cap, "cash": cap, "equity": cap, "peak_equity": cap,
        "positions": {}, "trades": [], "cooldowns": {}, "pause_until": 0, "consec_losses": 0,
        "killed": False, "killed_reason": None,
        "day": {"key": risk.day_key(now), "start_equity": cap, "trades": 0},
        "equity_curve": [], "last_report": {}, "cycles": 0,
    }


def load_state(path: str, cfg: dict, now: int) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return new_state(cfg, now)


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def fetch_features(client: D.BinanceData, sym: str) -> Optional[dict]:
    try:
        s = {"15m": client.klines(sym, "15m", 300), "1h": client.klines(sym, "1h", 300), "4h": client.klines(sym, "4h", 120)}
        f = F.build(s)
        return f if F.enough(f) else None
    except D.DataError:
        return None


def _equity(state: dict) -> float:
    return state["cash"] + sum(p["qty_open"] * p["mark"] for p in state["positions"].values())


def _blackout(cfg: dict, now: int) -> Optional[str]:
    for s in cfg.get("blackout_events_utc") or []:
        try:
            ts = int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            continue
        if ts - cfg["blackout_before_min"] * 60_000 <= now <= ts + cfg["blackout_after_min"] * 60_000:
            return f"فترة حظر حول حدث اقتصادي ({s})"
    return None


def _finish(state: dict, pos: dict, exit_type: str, now: int, cfg: dict) -> dict:
    rec = paper.close_record(pos, exit_type, now)
    state["trades"].append(rec)
    state["trades"] = state["trades"][-TRADES_MAX:]
    risk.on_trade_closed(state, rec["pnl"], pos["symbol"], exit_type in ("stop",), now, cfg)
    del state["positions"][pos["symbol"]]
    return rec


def run_cycle(client, cfg: dict, state: dict, now: int,
              news_fn: Callable = N.analyze) -> dict:
    rep: dict = {"ts": now, "actions": [], "errors": []}
    state["cycles"] += 1

    # ---- 1) universe + features ----
    try:
        tick = client.tickers_24h()
        universe = D.pick_universe(tick, cfg["universe"]["top_n"], cfg["universe"]["min_quote_volume_24h"],
                                   cfg["universe"]["exclude"], cfg["universe"]["include"])
    except D.DataError as e:
        rep["errors"].append(f"universe: {e}")
        state["last_report"] = rep
        return rep
    syms = list(dict.fromkeys(universe + list(state["positions"].keys()) + ["BTCUSDT"]))
    with ThreadPoolExecutor(max_workers=6) as ex:
        feats = dict(zip(syms, ex.map(lambda s: fetch_features(client, s), syms)))
    btc = feats.get("BTCUSDT")
    rep["universe"] = universe
    if btc is None:
        rep["errors"].append("BTC data unavailable — لا قرارات في الدورة دي")
        state["last_report"] = rep
        return rep

    # ---- 2) day roll + manage open positions ----
    dk = risk.day_key(now)
    cur_eq = _equity(state)
    if state["day"]["key"] != dk:
        state["day"] = {"key": dk, "start_equity": cur_eq, "trades": 0}
    for sym, pos in list(state["positions"].items()):
        f = feats.get(sym)
        if f is None:
            rep["errors"].append(f"{sym}: no data to manage position")
            continue
        m, h1 = f["15m"], f["1h"]
        i1 = F.idx_at(h1, now)
        atr_now = h1["atr"][i1] if i1 >= 0 else None
        closed_last_ot = None
        done = False
        for k in range(len(m["t"])):
            if m["ct"][k] < now:
                closed_last_ot = m["t"][k]
            if m["t"][k] <= pos["last_ot"]:
                continue
            for e in paper.process_candle(pos, m["o"][k], m["h"][k], m["l"][k], m["c"][k], m["t"][k], cfg, atr_now):
                state["cash"] += e["cash"]
                rep["actions"].append({"sym": sym, "type": e["type"], "price": round(e["price"], 6)})
                if pos["qty_open"] <= 1e-12:
                    rec = _finish(state, pos, e["type"], now, cfg)
                    rep["actions"][-1]["pnl"] = rec["pnl"]
                    done = True
                    break
            if done:
                break
        if done:
            continue
        if closed_last_ot:
            pos["last_ot"] = closed_last_ot
        last_px = m["c"][-1]
        pos["mark"] = last_px
        te = paper.time_exit(pos, now, last_px, cfg)
        if te:
            state["cash"] += te["cash"]
            rec = _finish(state, pos, te["type"], now, cfg)
            rep["actions"].append({"sym": sym, "type": te["type"], "price": round(last_px, 6), "pnl": rec["pnl"]})

    equity = _equity(state)
    state["equity"] = equity
    state["peak_equity"] = max(state["peak_equity"], equity)

    # ---- 3) news + regime + gate ----
    nws = news_fn(syms, cfg, now)
    rep["news"] = {k: nws.get(k) for k in ("ok", "global", "coins", "notes", "n", "llm", "summary", "failed")}
    reg = regime(btc, now, cfg)
    rep["regime"] = reg
    if reg.get("panic"):
        state["pause_until"] = max(state["pause_until"], now + int(cfg["regime"]["panic_pause_hours"] * HOUR))
    block = _blackout(cfg, now)
    ok_gate, gate_reason = risk.gate(state, equity, now, cfg)
    if not reg["ok"]:
        ok_gate, gate_reason = False, reg["reason"]
    elif block:
        ok_gate, gate_reason = False, block
    elif nws["enabled"] and nws["global"] <= cfg["news"]["veto_global_score"]:
        ok_gate, gate_reason = False, f"أخبار سلبية على السوق كله (score {nws['global']})"
    rep["gate"] = {"open": ok_gate, "reason": gate_reason}

    # ---- 4) candidates ----
    cands: List[dict] = []
    for sym in universe:
        f = feats.get(sym)
        if f is None or sym in state["positions"]:
            continue
        base = sym[:-4]
        ns = nws["coins"].get(base, 0.0)
        sc = score_symbol(sym, f, btc, now, cfg, ns)
        sc["news_score"] = ns
        if not sc["blocked"]:
            if nws["enabled"] and ns <= cfg["news"]["veto_symbol_score"]:
                sc["blocked"] = f"خبر سلبي على العملة (score {ns})"
            elif not sc.get("trigger_ok"):
                sc["blocked"] = "مفيش محفز دخول (كسر/ارتداد)"
            elif sc["score"] < cfg["entry"]["score_threshold"]:
                sc["blocked"] = f"النقاط {sc['score']} أقل من الحد {cfg['entry']['score_threshold']}"
        cands.append(sc)
    cands.sort(key=lambda x: (-(x["score"] if not x["blocked"] else x["score"] - 1000), x["symbol"]))

    # ---- 5) entries ----
    mult = risk.risk_multiplier(equity, state["peak_equity"], cfg)
    extra = 1.0 if (nws["ok"] or not nws["enabled"]) else 0.5
    opened = 0
    for sc in cands:
        sc["decision"] = sc["blocked"] or ""
        if sc["blocked"]:
            continue
        if not ok_gate:
            sc["decision"] = f"مرفوض: {gate_reason}"
            continue
        if opened >= 2:
            sc["decision"] = "حد الدخول في الدورة الواحدة"
            continue
        g, why = risk.gate(state, _equity(state), now, cfg, sc["symbol"])
        if not g:
            sc["decision"] = f"مرفوض: {why}"
            continue
        f = feats[sc["symbol"]]
        live = f["15m"]["c"][-1]
        if live > sc["price"] + 0.5 * sc["atr1"]:
            sc["decision"] = "السعر طلع بسرعة بعد الإشارة (مطاردة)"
            continue
        sc2 = dict(sc, price=live)
        plan = plan_trade(sc2, cfg)
        if plan is None or "blocked" in plan:
            sc["decision"] = (plan or {}).get("blocked", "no plan")
            continue
        eq_now = _equity(state)
        qty = risk.position_size(eq_now, state["cash"], plan["entry"] * (1 + cfg["costs"]["slippage"]),
                                 plan["stop"], cfg, mult, extra)
        if qty * live < 11:  # أقل من الحد الأدنى للأمر على بينانس
            sc["decision"] = "الحجم أقل من الحد الأدنى للأمر"
            continue
        last_closed = max((t for t, ct in zip(f["15m"]["t"], f["15m"]["ct"]) if ct < now), default=f["15m"]["t"][-1])
        pos = paper.new_position(sc["symbol"], live, qty, plan, now, last_closed, sc["score"], sc["reasons"], cfg)
        state["cash"] -= pos["cost_basis"] + pos["fees"]
        state["positions"][sc["symbol"]] = pos
        state["day"]["trades"] += 1
        opened += 1
        sc["decision"] = "✅ دخول"
        rep["actions"].append({"sym": sc["symbol"], "type": "entry", "price": round(pos["entry"], 6), "qty": qty,
                               "stop": round(pos["stop"], 6), "tp1": round(pos["tp1"], 6), "score": sc["score"],
                               "risk_usd": round(pos["risk_dist"] * qty, 2), "reasons": sc["reasons"]})

    rep["top"] = [{"symbol": c["symbol"], "score": c["score"], "parts": c.get("parts"), "decision": c.get("decision"),
                   "reasons": c.get("reasons"), "atr_pct": c.get("atr_pct")} for c in cands[:10]]
    rep["risk_mult"] = mult

    # ---- 6) final equity + curve ----
    equity = _equity(state)
    state["equity"] = equity
    state["peak_equity"] = max(state["peak_equity"], equity)
    state["equity_curve"].append([now, round(equity, 2)])
    state["equity_curve"] = state["equity_curve"][-CURVE_MAX:]
    state["last_report"] = rep
    return rep


def stats(state: dict) -> dict:
    tr = state["trades"]
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    gp, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    peak, mdd = 0.0, 0.0
    for _, e in state["equity_curve"]:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak * 100 if peak else 0)
    eq, d = state["equity"], state["day"]
    return {
        "equity": round(eq, 2), "initial": state["initial_capital"],
        "total_pnl": round(eq - state["initial_capital"], 2),
        "total_pnl_pct": round((eq / state["initial_capital"] - 1) * 100, 2),
        "today_pnl": round(eq - d["start_equity"], 2), "today_pnl_pct": round((eq / d["start_equity"] - 1) * 100, 2),
        "trades": len(tr), "win_rate": round(len(wins) / len(tr) * 100, 1) if tr else None,
        "profit_factor": round(gp / gl, 2) if gl > 0 else (None if not tr else 99.0),
        "avg_r": round(sum(t["r"] for t in tr) / len(tr), 2) if tr else None,
        "max_drawdown_pct": round(mdd, 2), "fees": round(sum(t["fees"] for t in tr), 2),
        "peak_equity": round(state["peak_equity"], 2),
    }


def snapshot(state: dict, cfg: dict) -> dict:
    return {
        "generated_ts": state["last_report"].get("ts"), "mode": cfg["mode"], "stats": stats(state),
        "killed": state["killed"], "killed_reason": state["killed_reason"],
        "positions": list(state["positions"].values()), "trades": state["trades"][-60:],
        "equity_curve": state["equity_curve"][-700:], "report": state["last_report"],
        "day": state["day"], "pause_until": state["pause_until"], "cycles": state["cycles"],
        "settings": {"risk": cfg["risk"], "entry": cfg["entry"], "exit": cfg["exit"], "costs": cfg["costs"]},
    }
