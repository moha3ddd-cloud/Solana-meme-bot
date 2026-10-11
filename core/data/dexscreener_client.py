"""
DexScreener API Client (Solana-only)
=====================================
Wrapper رقيق حول DexScreener REST API، مقصور على شبكة Solana بس.

مهم / حدود المصدر ده (اتقال في المحادثة، بس بنسجله هنا كمان عشان
أي حد يفتح الملف يعرف بسرعة):
- مجاني، من غير API key.
- Rate limits: 300 req/min لـendpoints الـpairs/tokens، 60 req/min
  لـendpoints الـprofiles/boosts.
- بيرجع بيانات "لحظة الطلب" بس — مفيش historical OHLC candles.
- مفيهوش: holder distribution, mint/freeze authority, honeypot test,
  buy/sell tax. دي محتاجة مصدر تاني (Solana RPC مباشرة، أو خدمة زي
  RugCheck/Helius) — شايفهم في core/data/onchain_client.py (لسه هنبنيه).

ملحوظة تشغيلية: الكود ده بيستخدم `requests` وبيتصل بـ
api.dexscreener.com فعليًا وقت التشغيل — مجرب هنا بس منطقيًا/statically
(بيئة التطوير الحالية معندهاش اتصال إنترنت خارجي)، فلازم تتأكد إنه
شغال عندك بتجربة حقيقية قبل ما تعتمد عليه.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests

SOLANA_CHAIN_ID = "solana"
BASE_URL = "https://api.dexscreener.com"

# حدود الـAPI الرسمية — بنحترمها هنا عشان مانتحظرش
PAIRS_RATE_LIMIT_PER_MIN = 300
PROFILES_RATE_LIMIT_PER_MIN = 60


class DexScreenerError(Exception):
    pass


class DexScreenerRateLimitError(DexScreenerError):
    pass


@dataclass
class _RateLimiter:
    """Rate limiter بسيط جدًا (sliding window) — مش محتاجين حاجة معقدة هنا."""
    max_per_minute: int
    _timestamps: list = None

    def __post_init__(self):
        self._timestamps = []

    def wait_if_needed(self) -> None:
        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 60]
        if len(self._timestamps) >= self.max_per_minute:
            sleep_time = 60 - (now - self._timestamps[0]) + 0.1
            if sleep_time > 0:
                time.sleep(sleep_time)
        self._timestamps.append(time.time())


class DexScreenerClient:
    def __init__(self, timeout_seconds: float = 10.0):
        self.timeout_seconds = timeout_seconds
        self._pairs_limiter = _RateLimiter(PAIRS_RATE_LIMIT_PER_MIN)
        self._profiles_limiter = _RateLimiter(PROFILES_RATE_LIMIT_PER_MIN)
        self._session = requests.Session()

    def _get(self, path: str, limiter: _RateLimiter, params: Optional[dict] = None) -> dict:
        limiter.wait_if_needed()
        url = f"{BASE_URL}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout_seconds)
        except requests.RequestException as e:
            raise DexScreenerError(f"network_error: {e}") from e

        if resp.status_code == 429:
            raise DexScreenerRateLimitError("dexscreener_rate_limited")
        if resp.status_code != 200:
            raise DexScreenerError(f"unexpected_status_{resp.status_code}: {resp.text[:200]}")

        try:
            return resp.json()
        except ValueError as e:
            raise DexScreenerError(f"invalid_json_response: {e}") from e

    # ------------------------------------------------------------------
    # Public methods — كلها مقصورة على Solana بس
    # ------------------------------------------------------------------
    def get_pairs_by_token_addresses(self, token_addresses: list[str]) -> list[dict]:
        """
        بيرجع كل الـpairs (على أي شبكة) لحد 30 توكن أدرس مرة واحدة.
        بنفلتر هنا على Solana بس عشان الكلاينت ده مخصص له.
        """
        if not token_addresses:
            return []
        if len(token_addresses) > 30:
            raise ValueError("dexscreener allows max 30 token addresses per request")

        joined = ",".join(token_addresses)
        data = self._get(f"/latest/dex/tokens/{joined}", self._pairs_limiter)
        pairs = data.get("pairs") or []
        return [p for p in pairs if p.get("chainId") == SOLANA_CHAIN_ID]

    def get_pair_by_address(self, pair_address: str) -> Optional[dict]:
        data = self._get(
            f"/latest/dex/pairs/{SOLANA_CHAIN_ID}/{pair_address}", self._pairs_limiter
        )
        pairs = data.get("pairs") or []
        return pairs[0] if pairs else None

    def search_pairs(self, query: str) -> list[dict]:
        """بحث بالاسم/الرمز — بنفلتر النتايج على Solana بس."""
        data = self._get("/latest/dex/search", self._pairs_limiter, params={"q": query})
        pairs = data.get("pairs") or []
        return [p for p in pairs if p.get("chainId") == SOLANA_CHAIN_ID]

    # ------------------------------------------------------------------
    # Discovery endpoints — دول أساس الـDiscoveryLayer (core/discovery.py).
    # DexScreener مفهوش "new pairs firehose" حقيقي في الـAPI المجاني
    # (زي ما اتوضح قبل كده)، لكن الـendpoints دول أقرب حاجة متاحة
    # لـ"عملات حديثة/نشطة دلوقتي":
    # - token-profiles: مشاريع سجّلت بيانات meta حديثًا (موقع، صورة...)
    # - token-boosts: عملات اشترت "تعزيز" ظهور على DexScreener نفسه —
    #   إشارة ضعيفة عمدًا (دفع فلوس مش إنجاز)، بس رخيصة ومتاحة مجانًا
    # ------------------------------------------------------------------
    def get_latest_token_profiles(self) -> list[dict]:
        data = self._get("/token-profiles/latest/v1", self._profiles_limiter, params=None)
        items = data if isinstance(data, list) else []
        return [p for p in items if p.get("chainId") == SOLANA_CHAIN_ID]

    def get_latest_token_boosts(self) -> list[dict]:
        data = self._get("/token-boosts/latest/v1", self._profiles_limiter, params=None)
        items = data if isinstance(data, list) else []
        return [p for p in items if p.get("chainId") == SOLANA_CHAIN_ID]
