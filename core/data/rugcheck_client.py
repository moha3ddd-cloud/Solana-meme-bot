"""
RugCheck API Client (api.rugcheck.xyz)
=========================================
اخترنا RugCheck بدل ما نبني الفحوصات دي من الصفر على Solana RPC مباشرة،
للسبب البسيط ده: RugCheck أصلًا بيعمل الشغل الصعب (parsing on-chain
accounts, حساب holder distribution, تتبع LP locks) وهو المعيار غير
الرسمي في مجتمع تداول Solana ("passed RugCheck" = حد أدنى من الفحص).
بناء البديل من الصفر يعني إعادة اختراع عجلة كبيرة (parsing SPL token
accounts, Raydium/Orca pool state...) من غير أي فايدة إضافية واضحة.

الـAPI:
- مجاني، من غير API key للـread endpoints.
- Rate limit: 10 requests/دقيقة من غير مفتاح، 60/دقيقة لو معاك مفتاح.
- Endpoint الأساسي: GET /v1/tokens/{mint}/report

⚠️ حدود مهمة (زي DexScreener بالظبط، نفس المبدأ):
- RugCheck **بيحلل بنية on-chain بس** — مش بيقيّم الفريق أو الـwhitepaper
  أو أي حاجة تسويقية.
- توكن شرعي وجديد ممكن ياخد تقييم سيء بسبب سيولة قليلة أو بنية غير
  تقليدية (false positive وارد).
- توكن خبيث ممكن ياخد تقرير "نضيف" لو الـauthorities اتلغت بعد ما
  الفريق اشترى مركزه (false negative وارد). يعني RugCheck شرط ضروري،
  مش شرط كافي — بالظبط زي ما الـScanner بتاعنا مصمم (فلتر + score،
  مش قرار نهائي أعمى).
- **مفيهوش honeypot test فعلي (simulate-sell)** — بيوفر مؤشرات (زي
  transfer fees) لكن مش اختبار بيع حقيقي. is_sellable الحقيقي محتاج
  simulate transaction فعلي (Phase الـExecution Engine).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

BASE_URL = "https://api.rugcheck.xyz/v1"
UNAUTHENTICATED_RATE_LIMIT_PER_MIN = 10
AUTHENTICATED_RATE_LIMIT_PER_MIN = 60


class RugCheckError(Exception):
    pass


class RugCheckRateLimitError(RugCheckError):
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


class RugCheckClient:
    def __init__(self, api_key: str = None, timeout_seconds: float = 10.0):
        """
        api_key اختياري — من غيره الـrate limit 10/دقيقة، بيه 60/دقيقة.
        سجّل مفتاح على rugcheck.xyz لو محتاج throughput أعلى.
        """
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        limit = AUTHENTICATED_RATE_LIMIT_PER_MIN if api_key else UNAUTHENTICATED_RATE_LIMIT_PER_MIN
        self._limiter = _RateLimiter(limit)
        self._session = requests.Session()

    def get_token_report(self, mint_address: str) -> dict:
        self._limiter.wait_if_needed()
        headers = {"X-API-KEY": self.api_key} if self.api_key else {}
        url = f"{BASE_URL}/tokens/{mint_address}/report"
        try:
            resp = self._session.get(url, headers=headers, timeout=self.timeout_seconds)
        except requests.RequestException as e:
            raise RugCheckError(f"network_error: {e}") from e

        if resp.status_code == 429:
            raise RugCheckRateLimitError("rugcheck_rate_limited")
        if resp.status_code == 404:
            raise RugCheckError("token_not_found_or_not_indexed_yet")
        if resp.status_code != 200:
            raise RugCheckError(f"unexpected_status_{resp.status_code}: {resp.text[:200]}")

        try:
            return resp.json()
        except ValueError as e:
            raise RugCheckError(f"invalid_json_response: {e}") from e
