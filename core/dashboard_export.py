"""
Dashboard Export
==================
بيحول حالة الـPaperTradingEngine (وبعدين الـLive Engine لما يتبني)
لـJSON واحد جاهز إن الـdashboard/index.html يفتحه.

مفيش backend server هنا — الـdashboard ملف HTML مستقل بيقرأ JSON
عادي. الاستخدام المتوقع: تشغل سكريبت بيعمل save_dashboard_snapshot()
كل شوية (أو بعد كل صفقة)، وتفتح dashboard/index.html وتحمّل آخر ملف.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from core.paper_trading import PaperTradingEngine


def _performance_by_day(closed_trades_with_time: list[dict]) -> list[dict]:
    """
    بيجمع الصفقات المقفولة حسب اليوم. متوقع إن كل عنصر فيه
    'closed_at' (ISO string) و'total_pnl_usd'.
    """
    by_day: dict[str, float] = {}
    for t in closed_trades_with_time:
        day = t.get("closed_at", "")[:10]
        if not day:
            continue
        by_day[day] = by_day.get(day, 0.0) + t.get("total_pnl_usd", 0.0)
    return [{"date": d, "pnl_usd": round(pnl, 2)} for d, pnl in sorted(by_day.items())]


def build_dashboard_snapshot(engine: PaperTradingEngine) -> dict:
    portfolio = engine.portfolio_snapshot()
    risk_status = portfolio["risk_engine_status"]

    open_positions = []
    for address, vp in engine.open_positions.items():
        pos = vp.position
        open_positions.append({
            "symbol": pos.symbol,
            "address": address,
            "profile": vp.profile,
            "entry_price": pos.entry_price,
            "current_stop_price": pos.current_stop_price,
            "remaining_size_pct": pos.remaining_size_pct,
            "targets_hit": pos.targets_hit,
            "trailing_active": pos.trailing_active,
            "entry_reasons": vp.entry_reasons,
            "opened_at": pos.opened_at.isoformat(),
        })

    trade_log = [
        {
            "timestamp": e.timestamp.isoformat(),
            "symbol": e.symbol,
            "address": e.address,
            "profile": e.profile,
            "action": e.action,
            "price": e.price,
            "size_usd": e.size_usd,
            "reason": e.reason,
            "pnl_usd": e.pnl_usd,
        }
        for e in engine.trade_log
    ]

    return {
        "generated_at": datetime.now().isoformat(),
        "mode": "paper",
        "portfolio": {
            "cash_usd": portfolio["cash_usd"],
            "open_positions_value_usd": portfolio["open_positions_value_usd"],
            "estimated_total_equity_usd": portfolio["estimated_total_equity_usd"],
            "starting_capital_usd": engine.risk_engine.config["account"]["starting_capital_usd"],
        },
        "risk_status": {
            "current_equity_usd": risk_status["current_equity_usd"],
            "peak_equity_usd": risk_status["peak_equity_usd"],
            "current_drawdown_pct": risk_status["current_drawdown_pct"],
            "drawdown_risk_multiplier": risk_status["drawdown_risk_multiplier"],
            "daily_pnl_usd": risk_status["daily_pnl_usd"],
            "daily_pnl_pct": risk_status["daily_pnl_pct"],
            "trades_opened_today": risk_status["trades_opened_today"],
            "trading_halted": risk_status["trading_halted"],
            "halt_reason": risk_status["halt_reason"],
            "kill_switch_active": risk_status["kill_switch_active"],
        },
        "drawdown_tiers": engine.risk_engine.config["drawdown_scaling"]["tiers"],
        "stats": {
            "open_positions_count": portfolio["open_positions_count"],
            "closed_trades_count": portfolio["closed_trades_count"],
            "win_rate_pct": portfolio["win_rate_pct"],
            "profit_factor": portfolio["profit_factor"],
            "by_profile": portfolio["by_profile"],
        },
        "open_positions": open_positions,
        "closed_trades": engine.closed_trades,
        "trade_log": trade_log,
        "config": {
            "risk_per_trade_pct": engine.risk_engine.config["position_sizing"]["risk_per_trade_pct"],
            "daily_loss_limit_pct": engine.risk_engine.config["daily_limits"]["daily_loss_limit_pct"],
            "max_trades_per_day": engine.risk_engine.config["daily_limits"]["max_trades_per_day"],
            "emergency_drawdown_pct": engine.risk_engine.config["emergency"]["emergency_drawdown_pct"],
        },
    }


def save_dashboard_snapshot(engine: PaperTradingEngine, path: str = "dashboard/snapshot.json") -> None:
    snapshot = build_dashboard_snapshot(engine)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
