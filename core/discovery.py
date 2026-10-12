"""
Discovery Layer
==================
مسؤوليته: "إيه العملات اللي تستاهل نفحصها بعمق دلوقتي؟" — مش أمان
ولا توقيت، ده الـScanner و SignalEngine بيعملوهم بعدين. هنا بس فلترة
رخيصة جدًا (DexScreener بس، من غير RugCheck أو Jupiter) عشان نوفّر
الـrate limit المحدود (RugCheck 10-60 طلب/دقيقة) لعملات تستاهل فعلًا.

⚠️ حد مهم اتقال قبل كده ولسه صحيح: DexScreener مفهوش "بث حي" حقيقي
لكل عملة جديدة على Solana. الملف ده بيستخدم أقرب حاجة متاحة مجانًا:
- token-profiles/latest: مشاريع سجّلت meta حديثًا
- token-boosts/latest: عملات "معزّزة" على DexScreener (دفع فلوس، مش
  إنجاز — إشارة ضعيفة عمدًا)
- + أي عناوين يدوية حطّيتها في watchlist بنفسك

يعني الـDiscovery ده **مكمّل**، مش شامل. عملة جديدة تمامًا لسه معندهاش
profile مسجل ومحدش عزّزها لسه ممكن تفوتك — ده قيد حقيقي، مش تقصير
في الكود.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

import yaml

from core.data.dexscreener_client import DexScreenerClient, SOLANA_CHAIN_ID


logger = logging.getLogger("discovery")


@dataclass
class DiscoveredCandidate:
    """عملة عدّت الفلترة الرخيصة — جاهزة تتبعت للـScanner/SignalEngine الكامل."""
    address: str
    symbol: str
    pair: dict  # الـpair JSON الخام من DexScreener — لسه محتاج build_dex_fields/mappers


class SeenTokensStore:
    """
    بيمنع إعادة فحص نفس العملة كل دورة من غير داعي (توفير requests).
    SQLite بسيط، بيفضل موجود بين إعادة تشغيل السكريبت.
    """

    def __init__(self, db_path: str = "data/discovery_seen.db"):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    address TEXT PRIMARY KEY,
                    last_seen_ts INTEGER NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def is_recently_seen(self, address: str, cooldown_minutes: int) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT last_seen_ts FROM seen_tokens WHERE address = ?", (address,))
            row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return False
        elapsed_minutes = (time.time() - row[0]) / 60
        return elapsed_minutes < cooldown_minutes

    def mark_seen(self, address: str) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                INSERT INTO seen_tokens (address, last_seen_ts) VALUES (?, ?)
                ON CONFLICT(address) DO UPDATE SET last_seen_ts = excluded.last_seen_ts
            """, (address, int(time.time())))
            conn.commit()
        finally:
            conn.close()


class DiscoveryLayer:
    def __init__(
        self,
        client: DexScreenerClient,
        seen_store: Optional[SeenTokensStore] = None,
        config_path: str = "config/discovery_config.yaml",
        geckoterminal_client=None,
    ):
        self.client = client
        self.seen_store = seen_store or SeenTokensStore()
        self.config_path = config_path
        # اختياري — لو موجود، new_pools/trending_pools بتاعته بتتضاف
        # كمصدر عناوين إضافي (شوف geckoterminal_mappers.pools_response_to_candidates)
        self.geckoterminal_client = geckoterminal_client
        # إحصائيات آخر discover(): بتتقرا من الـOrchestrator وبتتسجل في الـsnapshot
        # عشان نعرف سبب "مفيش صفقات": مفيش عملات اتكتشفت؟ ولا اتكتشفت واترفضت؟
        self.last_stats: dict = {}
        self._load_config()

    def _load_config(self) -> None:
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def reload_config(self) -> None:
        self._load_config()

    # ------------------------------------------------------------------
    # جمع العناوين المرشحة من كل المصادر المفعّلة
    # ------------------------------------------------------------------
    def _collect_candidate_addresses(self) -> list[str]:
        sources_cfg = self.config["discovery_sources"]
        addresses: list[str] = []

        if sources_cfg.get("use_token_profiles", True):
            try:
                profiles = self.client.get_latest_token_profiles()
                addresses.extend(p.get("tokenAddress") for p in profiles if p.get("tokenAddress"))
            except Exception as e:
                logger.warning(f"discovery_source_failed source=token_profiles error={e}")  # مصدر واحد فشل مايوقفش الباقي

        if sources_cfg.get("use_token_boosts", True):
            try:
                boosts = self.client.get_latest_token_boosts()
                addresses.extend(b.get("tokenAddress") for b in boosts if b.get("tokenAddress"))
            except Exception as e:
                logger.warning(f"discovery_source_failed source=token_boosts error={e}")

        addresses.extend(sources_cfg.get("manual_watchlist_addresses", []))

        # GeckoTerminal — مصدر إضافي (عناوين بس هنا، بنفس منطق توحيد
        # شكل البيانات عبر DexScreener لكل العملات بعد كده)
        if self.geckoterminal_client is not None:
            from core.data.geckoterminal_mappers import pools_response_to_candidates

            if sources_cfg.get("use_geckoterminal_new_pools", False):
                try:
                    response = self.geckoterminal_client.get_new_pools()
                    addresses.extend(c["address"] for c in pools_response_to_candidates(response) if c.get("address"))
                except Exception as e:
                    logger.warning(f"discovery_source_failed source=gecko_new_pools error={e}")

            if sources_cfg.get("use_geckoterminal_trending", False):
                try:
                    response = self.geckoterminal_client.get_trending_pools()
                    addresses.extend(c["address"] for c in pools_response_to_candidates(response) if c.get("address"))
                except Exception as e:
                    logger.warning(f"discovery_source_failed source=gecko_trending error={e}")

        # إزالة التكرار مع الحفاظ على الترتيب (أول ظهور)
        seen_in_batch = set()
        unique_addresses = []
        for addr in addresses:
            if addr and addr not in seen_in_batch:
                seen_in_batch.add(addr)
                unique_addresses.append(addr)
        return unique_addresses

    # ------------------------------------------------------------------
    # الفلترة الرخيصة على بيانات الـpair الفعلية
    # ------------------------------------------------------------------
    def _pre_filter_reason(self, pair: dict) -> Optional[str]:
        """بيرجع سبب الرفض (str) أو None لو العملة عدّت الفلترة."""
        cfg = self.config["pre_filter"]
        liquidity = (pair.get("liquidity") or {}).get("usd") or 0
        volume_5m = (pair.get("volume") or {}).get("m5") or 0
        pair_created_at_ms = pair.get("pairCreatedAt")

        if liquidity < cfg["min_liquidity_usd"]:
            return "low_liquidity"
        if volume_5m < cfg["min_volume_5m_usd"]:
            return "low_volume_5m"
        if pair_created_at_ms:
            age_minutes = (time.time() * 1000 - pair_created_at_ms) / 60000.0
            if age_minutes > cfg["max_token_age_minutes"]:
                return "too_old"
        return None

    def _passes_pre_filter(self, pair: dict) -> bool:
        return self._pre_filter_reason(pair) is None

    # ------------------------------------------------------------------
    # نقطة الدخول الرئيسية — بتتنادى دوريًا من الـMain Loop (لسه هنبنيه)
    # ------------------------------------------------------------------
    def discover(self) -> list[DiscoveredCandidate]:
        stats = {
            "addresses_collected": 0,
            "skipped_recently_seen": 0,
            "pairs_returned": 0,
            "pre_filter_rejected": {},
            "passed_pre_filter": 0,
            "batch_failures": 0,
        }
        self.last_stats = stats

        addresses = self._collect_candidate_addresses()
        stats["addresses_collected"] = len(addresses)
        if not addresses:
            return []

        cooldown = self.config["cooldown"]["recheck_cooldown_minutes"]
        max_candidates = self.config["limits"]["max_candidates_per_cycle"]

        # نفلتر الـaddresses اللي شفناها مؤخرًا قبل حتى ما نعمل batch request
        fresh_addresses = [a for a in addresses if not self.seen_store.is_recently_seen(a, cooldown)]
        stats["skipped_recently_seen"] = len(addresses) - len(fresh_addresses)
        if not fresh_addresses:
            return []

        results: list[DiscoveredCandidate] = []
        # DexScreener بياخد لحد 30 عنوان لكل request
        for i in range(0, len(fresh_addresses), 30):
            if len(results) >= max_candidates:
                break
            batch = fresh_addresses[i:i + 30]
            try:
                pairs = self.client.get_pairs_by_token_addresses(batch)
            except Exception as e:
                logger.warning(f"discovery_batch_failed error={e}")
                stats["batch_failures"] += 1
                continue  # batch فشل، نكمل اللي بعده بدل ما نوقف كله

            stats["pairs_returned"] += len(pairs)
            for pair in pairs:
                if len(results) >= max_candidates:
                    break
                base_token = pair.get("baseToken") or {}
                address = base_token.get("address")
                if not address:
                    continue

                self.seen_store.mark_seen(address)  # نسجل إننا شفناها، بغض النظر عن نتيجة الفلتر

                reason = self._pre_filter_reason(pair)
                if reason is not None:
                    stats["pre_filter_rejected"][reason] = stats["pre_filter_rejected"].get(reason, 0) + 1
                    continue

                results.append(DiscoveredCandidate(
                    address=address,
                    symbol=base_token.get("symbol", "UNKNOWN"),
                    pair=pair,
                ))

        stats["passed_pre_filter"] = len(results)
        return results
