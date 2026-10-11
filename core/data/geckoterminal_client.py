"""
GeckoTerminal API Client (CoinGecko's on-chain product)
==========================================================
السبب اللي خلانا نضيف المصدر ده: بيحل حاجتين كان عندنا فيهم فجوة
حقيقية بـDexScreener بس:

1. **Historical OHLCV حقيقي** — DexScreener مفهوش ده خالص (زي ما
   اتوضح من الأول). GeckoTerminal عنده شموع دقيقة/ساعة/يوم حقيقية،
   لحد 1000 شمعة. ده معناه نقدر نبدأ Backtesting من غير ما نستنى
   أسابيع نجمع بيانات بـDataLogger.
2. **اكتشاف عملات جديدة** — `new_pools` و`trending_pools` بديل
   (مش حقيقي 100% زي websocket مباشر، لكن أفضل من token-profiles/
   boosts بتاعة DexScreener).

الـAPI:
- مجاني بالكامل، من غير API key.
- Rate limit: 30 طلب/دقيقة (keyless) — أضيق من DexScreener (300)
  وRugCheck (10-60)، فده أهم مصدر نحافظ على استهلاكه بحكمة.
- شكل الردود JSON:API (data/attributes/relationships/included) —
  مختلف عن شكل DexScreener البسيط، فالـmappers في ملف منفصل
  (geckoterminal_mappers.py).

⚠️ الكود ده اتكتب صح حسب التوثيق المتاح، بس زي كل مصدر خارجي تاني في
المشروع، لازم يتجرب فعليًا على شبكة حقيقية قبل الاعتماد عليه.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

BASE_URL = "https://api.geckoterminal.com/api/v2"
SOLANA_NETWORK_ID = "solana"
RATE_LIMIT_PER_MIN = 30  # أضيق مصدر في المشروع — احترمه بحذر


class GeckoTerminalError(Exception):
    pass


class GeckoTerminalRateLimitError(GeckoTerminalError):
    pass


@dataclass
class _RateLimiter:
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


class GeckoTerminalClient:
    def __init__(self, timeout_seconds: float = 10.0):
        self.timeout_seconds = timeout_seconds
        self._limiter = _RateLimiter(RATE_LIMIT_PER_MIN)
        self._session = requests.Session()

    def _get(self, path: str, params: dict = None) -> dict:
        self._limiter.wait_if_needed()
        url = f"{BASE_URL}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout_seconds)
        except requests.RequestException as e:
            raise GeckoTerminalError(f"network_error: {e}") from e

        if resp.status_code == 429:
            raise GeckoTerminalRateLimitError("geckoterminal_rate_limited")
        if resp.status_code != 200:
            raise GeckoTerminalError(f"unexpected_status_{resp.status_code}: {resp.text[:200]}")

        try:
            return resp.json()
        except ValueError as e:
            raise GeckoTerminalError(f"invalid_json_response: {e}") from e

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def get_trending_pools(self) -> dict:
        """بيرجع الـraw JSON:API response — استخدم geckoterminal_mappers لتحويله."""
        return self._get(f"/networks/{SOLANA_NETWORK_ID}/trending_pools")

    def get_new_pools(self) -> dict:
        return self._get(f"/networks/{SOLANA_NETWORK_ID}/new_pools")

    def get_pool(self, pool_address: str) -> dict:
        return self._get(f"/networks/{SOLANA_NETWORK_ID}/pools/{pool_address}")

    def get_token_pools(self, token_address: str) -> dict:
        return self._get(f"/networks/{SOLANA_NETWORK_ID}/tokens/{token_address}/pools")

    # ------------------------------------------------------------------
    # Historical OHLCV — ده الإضافة الأهم (مش موجودة في DexScreener خالص)
    # ------------------------------------------------------------------
    def get_pool_ohlcv(
        self,
        pool_address: str,
        timeframe: str = "minute",
        aggregate: int = 5,
        limit: int = 100,
    ) -> dict:
        """
        timeframe: 'minute' | 'hour' | 'day'
        aggregate: حجم كل شمعة بوحدات الـtimeframe (مثلًا aggregate=5 مع
        timeframe='minute' = شمعة كل 5 دقايق)
        limit: أقصى 1000
        """
        if limit > 1000:
            raise ValueError("geckoterminal_max_ohlcv_limit_is_1000")
        params = {"aggregate": aggregate, "limit": limit}
        return self._get(f"/networks/{SOLANA_NETWORK_ID}/pools/{pool_address}/ohlcv/{timeframe}", params=params)
