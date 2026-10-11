"""
Transaction Signer
====================
الملف الوحيد في المشروع كله المفروض يشوف private key فعلي في الذاكرة.
كل حاجة تانية (Scanner, Strategy, ExecutionEngine, PaperTradingEngine)
بتوصل لحد unsigned transaction (base64) وتوقف — بالظبط زي ما اتفقنا
من الأول.

⚠️ يتطلب `pip install solders base58` — المكتبتين دول معملهومش تثبيت
في بيئة التطوير الحالية (الـsandbox معندهاش وصول كامل لـPyPI)، فالكود
هنا اتكتب صح حسب الـAPI الرسمي بس **متجربش فعليًا هنا**. جرّبه على
جهازك/سيرفرك قبل أي استخدام حقيقي.

مبادئ أمان تحكم الملف ده:
1. الـprivate key بييتحمل فقط وقت التوقيع، ومايتخزنش في أي attribute
   بيفضل موجود بعد كده من غير داعي.
2. مفيش أي print() أو logging.info() أو exception message فيه أي جزء
   من الـprivate key أو الـseed. لو exception حصل أثناء التوقيع، بنمسك
   بيه ونرمي رسالة عامة بدل ما نسيب الـtraceback الافتراضي يسرب حاجة.
3. dry_run=True (الافتراضي في الاستخدام المقترح) بيوقّع محليًا (عملية
   crypto بس، مالهاش علاقة بالشبكة أو الفلوس) من غير ما يبعت
   الـtransaction فعليًا — مفيد جدًا للتأكد إن كل حاجة شغالة قبل أي
   إرسال حقيقي.
4. الكلاس مايعملش أي retry تلقائي على broadcast فاشل — retry على
   transaction موقّعة ممكن يسبب double-spend في حالات نادرة، فالقرار
   ده لازم يبقى واعي من الطبقة اللي فوق، مش تلقائي هنا.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Optional, Protocol

import requests


class SignerError(Exception):
    pass


class BroadcastError(SignerError):
    pass


@dataclass
class SignedTransactionResult:
    signature: str
    broadcasted: bool
    dry_run: bool
    raw_signed_tx_base64: Optional[str] = None  # موجود بس لو dry_run، للمراجعة


class TransactionSigner(Protocol):
    """
    الواجهة اللي ExecutionEngine/PaperTradingEngine (لما يتحول لـLive)
    المفروض يتعاملوا معاها — من غير ما يعرفوا تفاصيل إزاي الـkey متخزن.
    """
    def sign_and_send(
        self, unsigned_tx_base64: str, rpc_url: str, dry_run: bool = True
    ) -> SignedTransactionResult: ...

    def public_key_str(self) -> str: ...


class _BaseSolanaSigner:
    """منطق مشترك بين طرق تحميل الـkey المختلفة — التوقيع والإرسال نفسهم مش هنا."""

    def _get_keypair(self):
        """كل subclass لازم يرجّع solders.keypair.Keypair. الدالة دي بس بتتنادى وقت الحاجة."""
        raise NotImplementedError

    def public_key_str(self) -> str:
        keypair = self._get_keypair()
        return str(keypair.pubkey())

    def sign_and_send(
        self, unsigned_tx_base64: str, rpc_url: str, dry_run: bool = True
    ) -> SignedTransactionResult:
        try:
            from solders.transaction import VersionedTransaction
        except ImportError as e:
            raise SignerError(
                "solders غير مثبت — شغّل: pip install solders base58"
            ) from e

        keypair = self._get_keypair()
        try:
            raw_unsigned = base64.b64decode(unsigned_tx_base64)
            unsigned_tx = VersionedTransaction.from_bytes(raw_unsigned)
            # النمط الرسمي من Jupiter لتوقيع transaction جاهزة من الـAPI:
            # إعادة بناء VersionedTransaction بنفس الـmessage مع الـsigner
            signed_tx = VersionedTransaction(unsigned_tx.message, [keypair])
            signed_bytes = bytes(signed_tx)
        except Exception as e:
            # رسالة عامة مقصودة — مانطبعش أي جزء من محتوى الـtransaction
            # أو الـkey في حالة الخطأ
            raise SignerError(f"signing_failed: {type(e).__name__}") from e
        finally:
            # مسح المرجع المحلي للـkeypair من الـscope ده بأسرع ما يمكن
            del keypair

        signed_b64 = base64.b64encode(signed_bytes).decode("ascii")
        signature = str(signed_tx.signatures[0])

        if dry_run:
            return SignedTransactionResult(
                signature=signature, broadcasted=False, dry_run=True,
                raw_signed_tx_base64=signed_b64,
            )

        self._broadcast(signed_b64, rpc_url)
        return SignedTransactionResult(signature=signature, broadcasted=True, dry_run=False)

    def _broadcast(self, signed_tx_base64: str, rpc_url: str) -> None:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                signed_tx_base64,
                {"encoding": "base64", "skipPreflight": False, "maxRetries": 0},
            ],
        }
        try:
            resp = requests.post(rpc_url, json=body, timeout=15.0)
        except requests.RequestException as e:
            raise BroadcastError(f"rpc_network_error: {e}") from e

        if resp.status_code != 200:
            raise BroadcastError(f"rpc_unexpected_status_{resp.status_code}")

        data = resp.json()
        if "error" in data:
            raise BroadcastError(f"rpc_error: {data['error']}")


class EnvKeypairSigner(_BaseSolanaSigner):
    """
    بيحمّل الـprivate key من متغير بيئة. أبسط طريقة، بس أقل أمانًا —
    أي حاجة عندها وصول لـenvironment variables بتاعت البروسيس (crash
    dumps, بعض أدوات الـmonitoring) ممكن تشوفها. مناسبة للتجربة/التطوير،
    مش موصى بيها لمبلغ حقيقي كبير — استخدم EncryptedKeystoreSigner بدلها.

    المتغير المتوقع فيه إما:
    - سلسلة base58 (زي ما بتظهر من Phantom/Solflare export)
    - أو JSON array من الأرقام (زي solana-keygen)
    """

    def __init__(self, env_var_name: str = "SOLANA_PRIVATE_KEY"):
        self.env_var_name = env_var_name

    def _get_keypair(self):
        try:
            from solders.keypair import Keypair
        except ImportError as e:
            raise SignerError("solders غير مثبت — شغّل: pip install solders base58") from e

        raw = os.environ.get(self.env_var_name)
        if not raw:
            raise SignerError(f"environment_variable_{self.env_var_name}_not_set")

        raw = raw.strip()
        try:
            if raw.startswith("["):
                key_bytes = bytes(json.loads(raw))
                return Keypair.from_bytes(key_bytes)
            return Keypair.from_base58_string(raw)
        except Exception as e:
            raise SignerError(f"invalid_private_key_format: {type(e).__name__}") from e


class EncryptedKeystoreSigner(_BaseSolanaSigner):
    """
    بيحمّل الـprivate key من ملف keystore مشفّر (core/wallet/keystore.py).
    الأمان أعلى من EnvKeypairSigner لأن الملف على القرص مشفّر أصلًا —
    حتى لو الملف اتسرق، عديم الفايدة من غير الباسورد.
    """

    def __init__(self, keystore_path: str, password: str):
        self.keystore_path = keystore_path
        self._password = password  # يفضل في الذاكرة لحد ما الـobject يتشال

    def _get_keypair(self):
        try:
            from solders.keypair import Keypair
        except ImportError as e:
            raise SignerError("solders غير مثبت — شغّل: pip install solders base58") from e

        from core.wallet.keystore import KeystoreError, WrongPasswordError, load_keystore

        try:
            secret_bytes = load_keystore(self._password, self.keystore_path)
        except WrongPasswordError as e:
            raise SignerError("wrong_keystore_password") from e
        except KeystoreError as e:
            raise SignerError(f"keystore_error: {e}") from e

        return Keypair.from_bytes(secret_bytes)


def get_sol_balance(public_key_str: str, rpc_url: str) -> float:
    """
    فحص رصيد read-only — آمن تمامًا، مش محتاج أي key. مفيد للتأكد إن
    الـwallet فيه فلوس كفاية قبل أي محاولة تنفيذ.
    """
    body = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [public_key_str]}
    resp = requests.post(rpc_url, json=body, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise SignerError(f"rpc_error_fetching_balance: {data['error']}")
    lamports = data["result"]["value"]
    return lamports / 1_000_000_000
