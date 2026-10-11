"""
Wallet Reputation Tracker
============================
⚠️ خليني أوضح إيه ده بالظبط عشان محدش يتوقع حاجة أكبر من الواقع:

ده **مش** Nansen ولا أي "smart money database" تجارية بتتبع محافظ
معروفة عبر آلاف المستخدمين. الخدمات دي مكلفة جدًا ومحتاجة بنية تحتية
ضخمة (تتبع كل transaction على السلسلة عبر آلاف المحافظ) خارج نطاق
مشروع فردي زي ده تمامًا.

اللي ده فعليًا: نظام بيتعلم من **صفقاتنا إحنا بنفسنا** بمرور الوقت.
كل مرة نقفل صفقة، بنسجل: مين كان من كبار الحاملين وقت ما دخلنا،
والنتيجة كانت ربح ولا خسارة. بعد شهور من التداول، لو نفس المحفظة
ظهرت في صفقات رابحة كتير، سمعتها تعلي — وده بيأثر على الـScore بتاع
عملة جديدة لو نفس المحفظة موجودة فيها من الأول.

**الفايدة الحقيقية:** صفر لحد ما نجمع بيانات كفاية من صفقاتنا إحنا.
مش أداة تدّيك edge من اليوم الأول — أداة بتتحسن مع الوقت لو استخدمناها
باستمرار. ده جزء من نفس فلسفة "قابل للاختبار" اللي المشروع كله مبني
عليها.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class WalletStats:
    address: str
    trades_seen: int
    wins: int
    total_pnl_usd: float

    @property
    def win_rate_pct(self) -> float:
        if self.trades_seen == 0:
            return 0.0
        return (self.wins / self.trades_seen) * 100


class WalletReputationTracker:
    MIN_TRADES_FOR_CONFIDENCE = 5  # تحت الرقم ده، السمعة مش موثوقة كفاية تأثر بقوة

    def __init__(self, db_path: str = "data/wallet_reputation.db"):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wallet_stats (
                    address TEXT PRIMARY KEY,
                    trades_seen INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    total_pnl_usd REAL NOT NULL DEFAULT 0.0
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def record_trade_outcome(self, top_holder_addresses: list[str], win: bool, pnl_usd: float) -> None:
        """
        بتتنادى لما صفقة تتقفل. top_holder_addresses لازم تكون اللي
        اتسجلت وقت الدخول (مش وقت الخروج — ممكن يكونوا اتغيروا).
        """
        if not top_holder_addresses:
            return
        conn = self._connect()
        try:
            for address in top_holder_addresses:
                conn.execute("""
                    INSERT INTO wallet_stats (address, trades_seen, wins, total_pnl_usd)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(address) DO UPDATE SET
                        trades_seen = trades_seen + 1,
                        wins = wins + excluded.wins,
                        total_pnl_usd = total_pnl_usd + excluded.total_pnl_usd
                """, (address, 1 if win else 0, pnl_usd))
            conn.commit()
        finally:
            conn.close()

    def get_wallet_stats(self, address: str) -> WalletStats:
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT trades_seen, wins, total_pnl_usd FROM wallet_stats WHERE address = ?",
                (address,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return WalletStats(address=address, trades_seen=0, wins=0, total_pnl_usd=0.0)
        return WalletStats(address=address, trades_seen=row[0], wins=row[1], total_pnl_usd=row[2])

    def has_reputation_data(self, addresses: list[str]) -> bool:
        """
        بيرجع True لو على الأقل محفظة واحدة من دول عندها تاريخ صفقات
        مسجل عندنا. الفرق بين "50 لأننا حسبنا وطلع متعادل" و"50 لأن
        مفيش بيانات خالص" مهم — الـScanner بيستخدم الدالة دي عشان
        يقرر هل يدّي وزن للإشارة دي أصلًا، أو يسيبها من غير تأثير.
        """
        return any(self.get_wallet_stats(a).trades_seen > 0 for a in addresses)

    def get_reputation_score(self, addresses: list[str]) -> float:
        """
        بيرجع 0-100. 50 = حيادي تمامًا (مفيش بيانات كفاية). فوق 50 =
        المحافظ دي شايفينها في صفقات رابحة قبل كده، تحت 50 = العكس.

        الثقة في النتيجة بتزيد مع عدد الصفقات اللي شفنا فيها كل محفظة —
        محفظة شفناها مرة واحدة بس متأثرش بقوة زي محفظة شفناها 20 مرة.
        """
        if not addresses:
            return 50.0

        weighted_sum = 0.0
        weight_total = 0.0
        for address in addresses:
            stats = self.get_wallet_stats(address)
            if stats.trades_seen == 0:
                continue
            # وزن الثقة بيزيد تدريجيًا مع عدد الصفقات، بيتشبع بعد MIN_TRADES_FOR_CONFIDENCE
            confidence = min(1.0, stats.trades_seen / self.MIN_TRADES_FOR_CONFIDENCE)
            weighted_sum += stats.win_rate_pct * confidence
            weight_total += confidence

        if weight_total == 0.0:
            return 50.0  # مفيش أي محفظة عندها تاريخ كفاية

        return round(weighted_sum / weight_total, 2)
