"""
Data Logger — بناء تاريخ خاص بينا من DexScreener
====================================================
بما إن DexScreener مالوش historical OHLC، الحل الوحيد اللي يخلينا
نـbacktest بجدية هو إننا نبدأ نسجل snapshots دورية بنفسنا من دلوقتي،
ونخزنها محليًا (SQLite — بسيط، مفيهوش أي setup إضافي).

بعد ما نجمع بيانات كفاية (أسابيع، مش أيام)، نقدر نحول الـsnapshots
لـOHLC candles ونبدأ الـBacktesting Engine (Phase 3) عليها فعليًا.

الاستخدام المتوقع:
    logger = DataLogger(db_path="data/snapshots.db")
    logger.init_db()
    logger.snapshot_pairs(client, token_addresses=[...])   # مرة كل دقيقة مثلًا
    candles = logger.export_ohlc("So1111...", interval_minutes=5)
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from core.data.dexscreener_client import DexScreenerClient


@dataclass
class OHLCCandle:
    address: str
    interval_start_ts: int
    open: float
    high: float
    low: float
    close: float
    volume_usd: float
    snapshot_count: int  # عدد الـsnapshots اللي اتبنى منها الـcandle — مؤشر للثقة في الدقة


class DataLogger:
    def __init__(self, db_path: str = "data/snapshots.db"):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pair_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT NOT NULL,
                    symbol TEXT,
                    captured_at_ts INTEGER NOT NULL,
                    price_usd REAL,
                    liquidity_usd REAL,
                    market_cap_usd REAL,
                    volume_5m_usd REAL,
                    volume_1h_usd REAL,
                    buys_5m INTEGER,
                    sells_5m INTEGER
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_address_time
                ON pair_snapshots(address, captured_at_ts)
            """)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Snapshotting
    # ------------------------------------------------------------------
    def snapshot_pairs(self, client: DexScreenerClient, token_addresses: list[str]) -> int:
        """
        بيسحب البيانات الحالية للتوكنز دي من DexScreener ويخزنها كـsnapshot.
        بيرجع عدد الـpairs اللي اتسجلوا فعليًا.

        المفروض الدالة دي تتنادى بشكل دوري (مثلًا كل دقيقة) من سكريبت
        خارجي أو scheduled job — الملف ده مايعملش الجدولة بنفسه.
        """
        if not token_addresses:
            return 0

        pairs = client.get_pairs_by_token_addresses(token_addresses)
        if not pairs:
            return 0

        now_ts = int(time.time())
        conn = self._connect()
        count = 0
        try:
            for pair in pairs:
                base_token = pair.get("baseToken") or {}
                liquidity = pair.get("liquidity") or {}
                volume = pair.get("volume") or {}
                txns_5m = (pair.get("txns") or {}).get("m5", {}) or {}

                conn.execute(
                    """
                    INSERT INTO pair_snapshots
                    (address, symbol, captured_at_ts, price_usd, liquidity_usd,
                     market_cap_usd, volume_5m_usd, volume_1h_usd, buys_5m, sells_5m)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        base_token.get("address", ""),
                        base_token.get("symbol", "UNKNOWN"),
                        now_ts,
                        float(pair.get("priceUsd") or 0.0),
                        float(liquidity.get("usd") or 0.0),
                        float(pair.get("marketCap") or pair.get("fdv") or 0.0),
                        float(volume.get("m5") or 0.0),
                        float(volume.get("h1") or 0.0),
                        int(txns_5m.get("buys") or 0),
                        int(txns_5m.get("sells") or 0),
                    ),
                )
                count += 1
            conn.commit()
        finally:
            conn.close()
        return count

    # ------------------------------------------------------------------
    # Reading back / building candles for backtesting later
    # ------------------------------------------------------------------
    def snapshot_count_for(self, address: str) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM pair_snapshots WHERE address = ?", (address,)
            )
            return cur.fetchone()[0]
        finally:
            conn.close()

    def date_range_covered(self, address: str) -> Optional[tuple[int, int]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT MIN(captured_at_ts), MAX(captured_at_ts) FROM pair_snapshots WHERE address = ?",
                (address,),
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                return (row[0], row[1])
            return None
        finally:
            conn.close()

    def export_ohlc(self, address: str, interval_minutes: int = 5) -> list[OHLCCandle]:
        """
        بيحول الـsnapshots المخزنة لـOHLC candles بحجم interval_minutes.
        الدقة هنا بتعتمد على كثافة الـsnapshots — لو كنت بتسجل كل دقيقة
        وعايز candles كل 5 دقايق، هتلاقي حوالي 5 نقاط لكل candle.
        لو snapshot_count قليل، اعتبر الـcandle ده مش دقيق كفاية للثقة فيه.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT captured_at_ts, price_usd, volume_5m_usd FROM pair_snapshots "
                "WHERE address = ? ORDER BY captured_at_ts ASC",
                (address,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            return []

        interval_seconds = interval_minutes * 60
        buckets: dict[int, list[tuple[int, float, float]]] = {}
        for ts, price, vol in rows:
            bucket_start = (ts // interval_seconds) * interval_seconds
            buckets.setdefault(bucket_start, []).append((ts, price, vol))

        candles: list[OHLCCandle] = []
        for bucket_start in sorted(buckets.keys()):
            points = buckets[bucket_start]
            prices = [p[1] for p in points]
            volumes = [p[2] for p in points]
            candles.append(OHLCCandle(
                address=address,
                interval_start_ts=bucket_start,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume_usd=sum(volumes),
                snapshot_count=len(points),
            ))
        return candles
