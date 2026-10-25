#!/usr/bin/env python3
"""فحص سريع: بينانس + فيدات الأخبار + (اختياري) Claude. شغّله قبل أول تشغيل حقيقي."""
import os, sys, time
import yaml
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from bot import data, features, news  # noqa: E402

cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
bad = 0
c = data.BinanceData()
try:
    t = c.tickers_24h(); u = data.pick_universe(t, cfg["universe"]["top_n"], cfg["universe"]["min_quote_volume_24h"], [], cfg["universe"]["include"])
    print(f"✅ Binance tickers: {len(t)} | universe={len(u)}: {u[:8]}…")
    s = {"15m": c.klines("BTCUSDT", "15m", 300), "1h": c.klines("BTCUSDT", "1h", 300), "4h": c.klines("BTCUSDT", "4h", 120)}
    f = features.build(s); print("✅ klines OK | enough bars:", features.enough(f), "| base used:", data.BASES[c._base_idx])
except Exception as e:  # noqa: BLE001
    print("❌ Binance:", e); bad += 1
now = int(time.time() * 1000)
r = news.fetch_headlines(cfg["news"]["feeds"], 24, now)
print(f"{'✅' if r['items'] else '❌'} News: {len(r['items'])} headlines (24h) | failed feeds: {r['failed']}")
bad += 0 if r["items"] else 1
print("ℹ️ Claude news scoring:", "مفعّل" if os.environ.get("ANTHROPIC_API_KEY") else "غير مفعّل (keywords بس)")
sys.exit(bad)
