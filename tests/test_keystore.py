"""
اختبارات الـKeystore — تشفير حقيقي (مكتبة cryptography)، من غير حاجة لـsolders.

تشغيل: python3 tests/test_keystore.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.wallet.keystore import (
    KeystoreError,
    WrongPasswordError,
    change_keystore_password,
    create_keystore,
    load_keystore,
)


def test_create_and_load_roundtrip():
    secret = b"\x01\x02\x03super-secret-key-bytes\x99"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "keystore.json")
        create_keystore(secret, "correct-horse-battery", path)
        loaded = load_keystore("correct-horse-battery", path)
        assert loaded == secret


def test_wrong_password_raises():
    secret = b"my-secret-key-data"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "keystore.json")
        create_keystore(secret, "correct-password", path)
        try:
            load_keystore("wrong-password", path)
            assert False, "expected WrongPasswordError"
        except WrongPasswordError:
            pass


def test_refuses_to_overwrite_existing_keystore():
    secret = b"secret-1"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "keystore.json")
        create_keystore(secret, "password-one-ok", path)
        try:
            create_keystore(b"secret-2", "password-two-ok", path)
            assert False, "expected KeystoreError"
        except KeystoreError:
            pass


def test_rejects_short_password():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "keystore.json")
        try:
            create_keystore(b"secret", "short", path)
            assert False, "expected KeystoreError"
        except KeystoreError:
            pass


def test_loading_nonexistent_keystore_raises():
    try:
        load_keystore("any-password", "/nonexistent/path/keystore.json")
        assert False, "expected KeystoreError"
    except KeystoreError:
        pass


def test_stored_file_does_not_contain_plaintext_secret():
    """أهم اختبار أمان: التأكد إن السر الخام مش موجود نص واضح في الملف."""
    secret = b"THIS_IS_A_VERY_RECOGNIZABLE_SECRET_MARKER_12345"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "keystore.json")
        create_keystore(secret, "some-password-here", path)
        with open(path, "rb") as f:
            raw_file_content = f.read()
        assert secret not in raw_file_content


def test_change_password_then_old_password_fails_new_succeeds():
    secret = b"rotate-me-secret"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "keystore.json")
        create_keystore(secret, "old-password-1", path)
        change_keystore_password("old-password-1", "new-password-2", path)

        try:
            load_keystore("old-password-1", path)
            assert False, "old password should no longer work"
        except WrongPasswordError:
            pass

        loaded = load_keystore("new-password-2", path)
        assert loaded == secret


def test_different_secrets_produce_different_ciphertext():
    """تأكيد إن مفيش نمط ثابت ممكن يكشف حاجة عن المحتوى."""
    with tempfile.TemporaryDirectory() as tmp:
        path1 = os.path.join(tmp, "ks1.json")
        path2 = os.path.join(tmp, "ks2.json")
        create_keystore(b"same-password-secret-A", "identical-password", path1)
        create_keystore(b"same-password-secret-A", "identical-password", path2)

        with open(path1) as f1, open(path2) as f2:
            content1, content2 = f1.read(), f2.read()
        # حتى بنفس السر ونفس الباسورد، لازم الملفين يختلفوا (salt عشوائي)
        assert content1 != content2


if __name__ == "__main__":
    import traceback

    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL: {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
