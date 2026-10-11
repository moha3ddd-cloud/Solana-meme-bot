"""
Encrypted Local Keystore
===========================
هدف الملف ده: منخزنش الـprivate key نص واضح على القرص أبدًا. بدل كده،
بنشفّره بباسورد إنت اخترته، والباسورد ده نفسه مش متخزن — لازم تدخله
في كل مرة تشغّل فيها البوت (أو تحطه في متغير بيئة منفصل عن ملف الـkeystore).

⚠️ الطبقة دي "minimal viable" مش enterprise-grade. لو رأس المال كبر
لدرجة تستاهل، فكر في:
- Hardware wallet (Ledger) مع توقيع خارجي
- KMS مُدار (AWS KMS, GCP KMS) بدل ملف محلي خالص
- Multi-sig لو أكتر من شخص شريك في القرار

طريقة العمل:
1. بنشتق مفتاح تشفير من الباسورد بتاعك عبر PBKDF2 (600,000 iteration،
   موصى بيه حاليًا) + salt عشوائي لكل keystore.
2. بنشفّر الـprivate key الخام بيه (Fernet — symmetric authenticated
   encryption، جزء من مكتبة cryptography الموثوقة).
3. بنخزن {salt, encrypted_data} في ملف JSON. من غير الباسورد الصح،
   الملف ده عديم الفايدة تمامًا.

**الباسورد نفسه ماتحطهوش في كود أو git. حطه في متغير بيئة وقت
التشغيل بس (export KEYSTORE_PASSWORD=...) أو ادخله يدويًا.**
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16


class KeystoreError(Exception):
    pass


class WrongPasswordError(KeystoreError):
    pass


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    derived = kdf.derive(password.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)  # Fernet محتاج base64-encoded 32-byte key


def create_keystore(secret_bytes: bytes, password: str, path: str) -> None:
    """
    بيشفّر secret_bytes (الـprivate key الخام) بالباسورد ويخزنه في path.
    بيرفض يكتب فوق ملف موجود عشان مايحصلش overwrite بالغلط لمفتاح موجود.
    """
    if os.path.exists(path):
        raise KeystoreError(f"keystore_already_exists_at_{path} — امسحه يدويًا لو متأكد إنك عايز تستبدله")
    if not password or len(password) < 8:
        raise KeystoreError("password_too_short — استخدم باسورد 8 حروف على الأقل")

    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive_key(password, salt)
    fernet = Fernet(key)
    encrypted = fernet.encrypt(secret_bytes)

    payload = {
        "version": 1,
        "kdf": "pbkdf2_sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "encrypted_data": base64.b64encode(encrypted).decode("ascii"),
    }

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    # صلاحيات القراءة/الكتابة للمالك بس (مش متاحة كلها لأي حد على النظام)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # بعض الأنظمة (زي Windows) مش بتدعم chmod بنفس الطريقة


def load_keystore(password: str, path: str) -> bytes:
    """بيرجع الـprivate key الخام (bytes) لو الباسورد صح، وإلا بيرمي WrongPasswordError."""
    if not os.path.exists(path):
        raise KeystoreError(f"keystore_not_found_at_{path}")

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    salt = base64.b64decode(payload["salt"])
    key = _derive_key(password, salt)
    fernet = Fernet(key)

    try:
        return fernet.decrypt(base64.b64decode(payload["encrypted_data"]))
    except InvalidToken as e:
        raise WrongPasswordError("wrong_password_or_corrupted_keystore") from e


def change_keystore_password(old_password: str, new_password: str, path: str) -> None:
    """بيغيّر الباسورد من غير ما يغيّر الـprivate key نفسه."""
    secret = load_keystore(old_password, path)
    os.remove(path)
    try:
        create_keystore(secret, new_password, path)
    except Exception:
        # لو فشل، رجّع الملف القديم بدل ما تسيب المستخدم من غير keystore خالص
        create_keystore(secret, old_password, path)
        raise
