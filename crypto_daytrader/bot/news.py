"""News layer: بيسحب عناوين RSS، ويقيّمها (keywords + اختياري Claude)، ويشتغل كـ**فيتو** مش زرار شراء.

- score عالمي (السوق كله) + score لكل عملة. سالب كبير → ما فيش دخول.
- الأخبار القديمة وزنها بيقل (half-life).
- لو الفيدات وقعت: news_ok=False والبوت يقلل المخاطرة للنص بدل ما يتصرف كأن مفيش أخبار سيئة.
"""
from __future__ import annotations

import json
import math
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional

import requests

ALIASES = {
    "BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "ether", "eth"], "BNB": ["bnb", "binance coin", "bnb chain"],
    "SOL": ["solana", "sol"], "XRP": ["xrp", "ripple"], "DOGE": ["dogecoin", "doge"], "ADA": ["cardano", "ada"],
    "AVAX": ["avalanche", "avax"], "LINK": ["chainlink", "link"], "TRX": ["tron", "trx"], "DOT": ["polkadot", "dot"],
    "LTC": ["litecoin", "ltc"], "SUI": ["sui"], "TON": ["toncoin", "ton"], "NEAR": ["near protocol", "near"],
    "ARB": ["arbitrum", "arb"], "OP": ["optimism"], "APT": ["aptos", "apt"], "SHIB": ["shiba", "shib"],
    "PEPE": ["pepe"], "UNI": ["uniswap", "uni"], "AAVE": ["aave"], "ATOM": ["cosmos", "atom"], "HBAR": ["hedera", "hbar"],
}

# (regex, وزن) — سالب = خطر
NEG_STRONG = [r"\bhack(ed|s)?\b", r"\bexploit(ed|s)?\b", r"\bdrain(ed|s)?\b", r"\brug ?pull", r"\bbankrupt", r"\binsolven",
              r"\bdelist", r"halts? (withdraw|deposit|trading)", r"suspends? (withdraw|deposit|trading)", r"\bsec (sues|charges)",
              r"\bsued\b", r"\bbans?\b", r"\bban(ned)?\b", r"\bseiz(e|ed|ure)", r"\bde-?peg", r"\bfraud", r"\bcollapse"]
NEG_MILD = [r"\bcrash", r"\bplunge", r"\bplummet", r"\bsell-?off", r"\bliquidations?\b", r"\boutage", r"\binvestigat",
            r"\bprobe\b", r"\bfine[sd]?\b", r"\bwarns?\b", r"\blawsuit", r"\bfears?\b", r"\bdump", r"\btumble", r"\boutflows?\b"]
POS = [r"\betf (approv|inflow)", r"\bapprov(ed|al)\b", r"\bpartnership", r"\bmainnet\b", r"\bupgrade\b", r"\blists?\b.*\bbinance",
       r"\badoption", r"\brecord high", r"\ball-?time high", r"\binflows?\b", r"\brall(y|ies)", r"\bsurge", r"\bbullish", r"\bsoars?\b"]
GLOBAL_HINT = re.compile(r"\b(crypto|market|exchange|stablecoin|fed|sec|cftc|etf|regulat|tether|usdt|binance|coinbase|bitcoin)\b", re.I)


def _cnt(patterns: List[str], text: str) -> int:
    return sum(1 for p in patterns if re.search(p, text, re.I))


def fetch_headlines(feeds: List[str], lookback_h: float, now_ms: int, timeout: float = 12.0) -> Dict[str, list]:
    items, failed = [], []
    for url in feeds:
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 crypto-daytrader"})
            if r.status_code != 200:
                failed.append(f"{url} HTTP {r.status_code}")
                continue
            root = ET.fromstring(r.content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                pub = it.findtext("pubDate")
                try:
                    ts = int(parsedate_to_datetime(pub).timestamp() * 1000) if pub else now_ms
                except Exception:
                    ts = now_ms
                if title and now_ms - ts <= lookback_h * 3_600_000 and ts <= now_ms + 600_000:
                    items.append({"title": title, "ts": ts, "src": url.split("/")[2]})
        except Exception as e:  # noqa: BLE001 — أي فيد يقع ما يوقفش الدورة
            failed.append(f"{url} {e.__class__.__name__}")
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: -x["ts"]):
        k = re.sub(r"\W+", "", it["title"].lower())[:60]
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    return {"items": uniq, "failed": failed}


def score_keywords(items: List[dict], symbols: List[str], now_ms: int, half_life_h: float) -> dict:
    glob = 0.0
    coins: Dict[str, float] = {}
    notes: List[str] = []
    bases = {s[:-4]: s for s in symbols if s.endswith("USDT")}
    for it in items:
        text = it["title"]
        age_h = max(0.0, (now_ms - it["ts"]) / 3_600_000)
        decay = 0.5 ** (age_h / half_life_h)
        raw = -3.0 * min(_cnt(NEG_STRONG, text), 1) - 1.5 * min(_cnt(NEG_MILD, text), 1) + 1.0 * min(_cnt(POS, text), 1)
        if raw == 0:
            continue
        matched = False
        low = text.lower()
        for base in bases:
            names = ALIASES.get(base, [base.lower()])
            if any(re.search(rf"\b{re.escape(n)}\b", low) for n in names):
                coins[base] = coins.get(base, 0.0) + raw * decay
                matched = True
        if not matched or (raw < 0 and GLOBAL_HINT.search(text) and _cnt(NEG_STRONG, text)):
            # خبر عام (SEC / بورصة كبيرة / ...) — نسبته للسوق كله، بوزن أقل
            if GLOBAL_HINT.search(text):
                glob += raw * decay * (0.7 if matched else 1.0)
        if raw <= -1.5 and abs(raw * decay) >= 1.0 and len(notes) < 6:
            notes.append(text[:110])
    return {"global": round(glob, 2), "coins": {k: round(v, 2) for k, v in coins.items()}, "notes": notes}


def llm_assess(items: List[dict], symbols: List[str], model: str, timeout: float = 40.0) -> Optional[dict]:
    """اختياري: Claude يقيّم العناوين. لو مفيش مفتاح أو فشل → None (والـkeywords تكمل)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not items:
        return None
    heads = "\n".join(f"- {it['title']}" for it in items[:40])
    bases = [s[:-4] for s in symbols]
    prompt = (
        "You are a cautious crypto risk analyst. Given the recent headlines, rate how they affect SHORT-TERM (next 24h) "
        "long-only spot trading risk. Score from -3 (severe: exchange hack, major ban, depeg, systemic crash) to +3 (clearly bullish "
        "and material). Most headlines are noise: use 0 unless materially important. Reply ONLY with JSON: "
        '{"global": <number>, "coins": {"BTC": <number>, ...}, "summary": "<one short sentence>"}. '
        f"Only include coins from this list if specifically affected: {bases}.\n\nHeadlines:\n{heads}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages", timeout=timeout,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]})
        if r.status_code != 200:
            return None
        txt = r.json()["content"][0]["text"]
        m = re.search(r"\{.*\}", txt, re.S)
        d = json.loads(m.group(0))
        return {"global": float(d.get("global", 0)), "coins": {k: float(v) for k, v in (d.get("coins") or {}).items()},
                "summary": str(d.get("summary", ""))[:200]}
    except Exception:  # noqa: BLE001
        return None


def analyze(symbols: List[str], cfg: dict, now_ms: int) -> dict:
    nc = cfg["news"]
    if not nc.get("enabled", True):
        return {"ok": True, "enabled": False, "global": 0.0, "coins": {}, "notes": [], "n": 0, "llm": False}
    res = fetch_headlines(nc["feeds"], nc["lookback_hours"], now_ms)
    items = res["items"]
    ok = len(items) > 0
    kw = score_keywords(items, symbols, now_ms, nc["half_life_hours"])
    glob, coins, notes = kw["global"], dict(kw["coins"]), list(kw["notes"])
    llm = llm_assess(items, symbols, nc.get("llm_model", "claude-haiku-5-5")) if nc.get("use_llm") else None
    summary = None
    if llm:
        glob = min(glob, llm["global"]) if llm["global"] < 0 else (glob + llm["global"]) / 2
        for k, v in llm["coins"].items():
            cur = coins.get(k, 0.0)
            coins[k] = min(cur, v) if v < 0 else (cur + v) / 2
        summary = llm.get("summary")
    return {"ok": ok, "enabled": True, "global": round(glob, 2), "coins": {k: round(v, 2) for k, v in coins.items()},
            "notes": notes, "n": len(items), "failed": res["failed"], "llm": bool(llm), "summary": summary}
