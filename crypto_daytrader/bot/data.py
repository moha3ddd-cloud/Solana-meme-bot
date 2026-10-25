"""Binance public market data (no API key needed — paper trading only).

بيجرب data-api.binance.vision الأول (مخصص للبيانات العامة وما بيتحجبش
جغرافياً) وبعدين api.binance.com كاحتياطي.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

import requests

BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api.binance.us",
]
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}

STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDE", "EUR", "EURI", "AEUR", "PAXG", "XUSD", "BFUSD", "USD1"}
LEVERAGED_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")


class DataError(RuntimeError):
    pass


def empty_series() -> Dict[str, list]:
    return {"t": [], "o": [], "h": [], "l": [], "c": [], "v": [], "ct": []}


def rows_to_series(rows: list) -> Dict[str, list]:
    s = empty_series()
    for r in rows:
        s["t"].append(int(r[0]))
        s["o"].append(float(r[1]))
        s["h"].append(float(r[2]))
        s["l"].append(float(r[3]))
        s["c"].append(float(r[4]))
        s["v"].append(float(r[5]))
        s["ct"].append(int(r[6]))
    return s


class BinanceData:
    def __init__(self, timeout: float = 15.0, session: Optional[requests.Session] = None):
        self.timeout = timeout
        self.s = session or requests.Session()
        self._base_idx = 0

    def _get(self, path: str, params: Optional[dict] = None):
        last = None
        for k in range(len(BASES)):
            base = BASES[(self._base_idx + k) % len(BASES)]
            for attempt in range(2):
                try:
                    r = self.s.get(base + path, params=params, timeout=self.timeout)
                    if r.status_code == 200:
                        self._base_idx = (self._base_idx + k) % len(BASES)
                        return r.json()
                    last = f"{base}{path} -> HTTP {r.status_code}"
                    if r.status_code in (418, 429):
                        time.sleep(2 + attempt * 3)
                        continue
                    break  # 4xx/451 غير مؤقت: جرب base تاني
                except requests.RequestException as e:
                    last = f"{base}{path} -> {e.__class__.__name__}"
                    time.sleep(0.5)
        raise DataError(last or "no response")

    def klines(self, symbol: str, interval: str, limit: int = 500,
               start: Optional[int] = None, end: Optional[int] = None) -> Dict[str, list]:
        p = {"symbol": symbol, "interval": interval, "limit": limit}
        if start is not None:
            p["startTime"] = int(start)
        if end is not None:
            p["endTime"] = int(end)
        return rows_to_series(self._get("/api/v3/klines", p))

    def klines_range(self, symbol: str, interval: str, start: int, end: int) -> Dict[str, list]:
        """Paginated download (1000 per request) — used by the backtest."""
        out = empty_series()
        cur = start
        step = INTERVAL_MS[interval]
        while cur < end:
            part = self.klines(symbol, interval, limit=1000, start=cur, end=end)
            if not part["t"]:
                break
            for k in out:
                out[k].extend(part[k])
            nxt = part["t"][-1] + step
            if nxt <= cur:
                break
            cur = nxt
            if len(part["t"]) < 1000:
                break
        return out

    def tickers_24h(self) -> List[dict]:
        return self._get("/api/v3/ticker/24hr")

    def prices(self) -> Dict[str, float]:
        return {x["symbol"]: float(x["price"]) for x in self._get("/api/v3/ticker/price")}


def pick_universe(tickers: List[dict], top_n: int, min_quote_volume: float,
                  exclude: List[str], include: List[str]) -> List[str]:
    """أعلى N زوج USDT من حيث السيولة، من غير stablecoins ولا leveraged tokens."""
    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in STABLES or base in exclude:
            continue
        if any(base.endswith(sfx) and len(base) > len(sfx) + 1 for sfx in LEVERAGED_SUFFIX):
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
        except (TypeError, ValueError):
            continue
        if qv >= min_quote_volume:
            rows.append((qv, sym))
    rows.sort(reverse=True)
    syms = [s for _, s in rows[:top_n]]
    for s in include:
        if s not in syms:
            syms.append(s)
    return syms
