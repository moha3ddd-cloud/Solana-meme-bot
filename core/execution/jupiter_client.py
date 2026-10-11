"""
Jupiter Aggregator Client (api.jup.ag)
=========================================
Jupiter لسه الاختيار الأوضح لـswap routing على Solana في 2026 —
بيغطي أكتر من 70% من حجم الـaggregator-routed swaps على الشبكة،
ومدمج افتراضيًا في معظم المحافظ (Phantom, Backpack, Solflare).
مفيش بديل واضح أحسن حاليًا، فمشيناه.

⚠️ تغيير مهم في 2026: Jupiter بيرحّل تدريجيًا من `lite-api.jup.ag`
(مجاني من غير مفتاح) لـ`api.jup.ag` (محتاج API key حتى للـFree tier).
سجّل مفتاح مجاني من https://portal.jup.ag قبل ما تستخدم الكلاينت ده.
لو معاك مفتاح، حطه في `JUPITER_API_KEY`.

⚠️ الكلاينت ده **بيوقف عند حدود التوقيع بالظبط زي ما اتفقنا**:
- `get_quote()` -> بيرجع أسعار وroute بس، من غير أي التزام
- `get_swap_transaction()` -> بيرجع transaction **غير موقّعة** (base64)
لا الكلاينت ده ولا أي حاجة تانية في المشروع بتلمس private keys أو
توقيع transactions. التوقيع والإرسال الفعلي هيبقى مسؤولية طبقة
Wallet/Signer منفصلة تمامًا (لسه هنتكلم فيها بعدين زي ما اتفقنا).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests

SOL_MINT = "So11111111111111111111111111111111111111112"

DEFAULT_BASE_URL = "https://api.jup.ag"


class JupiterError(Exception):
    pass


class JupiterRateLimitError(JupiterError):
    pass


class JupiterNoRouteError(JupiterError):
    """مفيش route متاح للـswap ده — ممكن يستخدم كإشارة honeypot/سيولة ميتة."""
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


class JupiterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        requests_per_minute: int = 60,
        timeout_seconds: float = 10.0,
    ):
        """
        api_key: مطلوب فعليًا في 2026 حتى للـFree tier (portal.jup.ag).
        requests_per_minute: عدّله حسب الـtier بتاعك في الـportal.
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._limiter = _RateLimiter(requests_per_minute)
        self._session = requests.Session()

    def _headers(self) -> dict:
        # ملحوظة: توثيق Jupiter مايحددش اسم الـheader بالظبط في كل
        # الأماكن — أكّد من dashboard البورتال بتاعك. x-api-key الأشيع.
        return {"x-api-key": self.api_key} if self.api_key else {}

    def _get(self, path: str, params: dict) -> dict:
        self._limiter.wait_if_needed()
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, params=params, headers=self._headers(), timeout=self.timeout_seconds)
        except requests.RequestException as e:
            raise JupiterError(f"network_error: {e}") from e

        if resp.status_code == 429:
            raise JupiterRateLimitError("jupiter_rate_limited")
        if resp.status_code != 200:
            raise JupiterError(f"unexpected_status_{resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
        except ValueError as e:
            raise JupiterError(f"invalid_json_response: {e}") from e

        if not data or "outAmount" not in data:
            raise JupiterNoRouteError(f"no_route_found: {data}")
        return data

    def _post(self, path: str, json_body: dict) -> dict:
        self._limiter.wait_if_needed()
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.post(url, json=json_body, headers=self._headers(), timeout=self.timeout_seconds)
        except requests.RequestException as e:
            raise JupiterError(f"network_error: {e}") from e

        if resp.status_code == 429:
            raise JupiterRateLimitError("jupiter_rate_limited")
        if resp.status_code != 200:
            raise JupiterError(f"unexpected_status_{resp.status_code}: {resp.text[:200]}")

        try:
            return resp.json()
        except ValueError as e:
            raise JupiterError(f"invalid_json_response: {e}") from e

    # ------------------------------------------------------------------
    def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 100,
    ) -> dict:
        """
        amount بوحدات الـtoken الأصغر (lamports للـSOL، أصغر وحدة حسب
        الـdecimals للتوكن التاني). بيرمي JupiterNoRouteError لو مفيش
        route — ده مفيد كـإشارة أولية إن السيولة ميتة أو honeypot.
        """
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
        }
        return self._get("/swap/v1/quote", params)

    def get_swap_transaction(self, quote_response: dict, user_public_key: str) -> dict:
        """
        بيرجع dict فيه 'swapTransaction' (base64، **غير موقّعة**).
        الخطوة اللي بعدها (فك التشفير، التوقيع، الإرسال للشبكة) برّه
        نطاق الكلاينت ده تمامًا — مسؤولية طبقة الـWallet/Signer.
        """
        body = {
            "quoteResponse": quote_response,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
        }
        return self._post("/swap/v1/swap", body)
