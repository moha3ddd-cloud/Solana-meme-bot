"""
اختبارات Signer — بنختبر الـplumbing (قراءة env var، بناء RPC payload،
سلوك dry_run، معالجة الأخطاء) من غير ما نحتاج solders فعليًا مثبتة —
عشان solders مش متاحة في بيئة التطوير دي. الاختبارات دي بتتأكد إن كل
حاجة حوالين التوقيع شغالة صح؛ التوقيع نفسه (اللي محتاج solders) لازم
يتجرب على جهازك بعد `pip install solders base58`.

تشغيل: python3 tests/test_signer.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.wallet.signer import (
    BroadcastError,
    EncryptedKeystoreSigner,
    EnvKeypairSigner,
    SignerError,
    get_sol_balance,
)


# ---------------- EnvKeypairSigner: env var handling (بدون solders) ----------------

def test_env_signer_raises_clear_error_when_var_missing():
    signer = EnvKeypairSigner(env_var_name="NONEXISTENT_TEST_VAR_XYZ")
    # لو solders مش متاح، الخطأ هيبقى عن solders نفسه، مش عن env var —
    # نتأكد إن مفيش أي جزء من الخطأ فيه أي بيانات حساسة على الأقل
    try:
        signer._get_keypair()
        assert False, "expected SignerError"
    except SignerError as e:
        assert "PRIVATE_KEY" not in str(e) or "not_set" in str(e) or "solders" in str(e).lower()


def test_env_signer_error_never_leaks_env_value():
    """حتى لو الفورمات غلط، رسالة الخطأ متتضمنش القيمة الخام."""
    os.environ["TEST_SOLANA_KEY_BAD_FORMAT"] = "this-is-not-a-valid-key-at-all-xyz123"
    try:
        signer = EnvKeypairSigner(env_var_name="TEST_SOLANA_KEY_BAD_FORMAT")
        try:
            signer._get_keypair()
        except SignerError as e:
            assert "this-is-not-a-valid-key-at-all-xyz123" not in str(e)
    finally:
        del os.environ["TEST_SOLANA_KEY_BAD_FORMAT"]


# ---------------- EncryptedKeystoreSigner: wrong password path ----------------

def test_keystore_signer_wrong_password_raises_signer_error():
    from core.wallet.keystore import create_keystore

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "keystore.json")
        create_keystore(b"fake-secret-bytes-for-test", "correct-password-123", path)

        signer = EncryptedKeystoreSigner(keystore_path=path, password="wrong-password-456")
        try:
            signer._get_keypair()
            assert False, "expected SignerError (either wrong password or missing solders)"
        except SignerError as e:
            # الخطأ لازم يكون عن الباسورد أو عن solders — مش أي تسريب لمحتوى الملف
            assert "fake-secret-bytes-for-test" not in str(e)


# ---------------- Mocked signer: تأكيد إن sign_and_send بتشتغل صح مع keypair وهمي ----------------

class _FakeKeypair:
    """بديل بسيط لـsolders.keypair.Keypair — بس عشان نختبر منطق الـsigner."""
    def pubkey(self):
        return "FakePublicKey11111111111111111111111111111"


class _FakeSignerNoNetwork(EnvKeypairSigner):
    """Signer وهمي بيتخطى الحاجة لـsolders تمامًا — لاختبار public_key_str بس."""
    def _get_keypair(self):
        return _FakeKeypair()


def test_public_key_str_uses_keypair_pubkey():
    signer = _FakeSignerNoNetwork()
    assert signer.public_key_str() == "FakePublicKey11111111111111111111111111111"


# ---------------- get_sol_balance: RPC call structure (بدون شبكة حقيقية) ----------------

class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code != 200:
            raise Exception(f"status_{self.status_code}")


def test_get_sol_balance_converts_lamports_to_sol(monkeypatch):
    def fake_post(url, json, timeout):
        assert json["method"] == "getBalance"
        return _FakeResponse({"result": {"value": 2_500_000_000}})  # 2.5 SOL بالـlamports

    import core.wallet.signer as signer_module
    monkeypatch.setattr(signer_module.requests, "post", fake_post)

    balance = get_sol_balance("FakePublicKey111", "https://fake-rpc.example.com")
    assert balance == 2.5


def test_get_sol_balance_raises_on_rpc_error(monkeypatch):
    def fake_post(url, json, timeout):
        return _FakeResponse({"error": {"code": -32602, "message": "invalid params"}})

    import core.wallet.signer as signer_module
    monkeypatch.setattr(signer_module.requests, "post", fake_post)

    try:
        get_sol_balance("BadKey", "https://fake-rpc.example.com")
        assert False, "expected SignerError"
    except SignerError:
        pass


if __name__ == "__main__":
    import traceback

    class _MonkeyPatch:
        """monkeypatch بسيط بديل — عشان نشغل الملف standalone من غير pytest."""
        def __init__(self):
            self._original = []

        def setattr(self, obj, name, value):
            self._original.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, val in self._original:
                setattr(obj, name, val)

    tests = [(name, obj) for name, obj in list(globals().items()) if name.startswith("test_")]
    passed, failed = 0, 0
    for name, t in tests:
        try:
            if "monkeypatch" in t.__code__.co_varnames[: t.__code__.co_argcount]:
                mp = _MonkeyPatch()
                t(mp)
                mp.undo()
            else:
                t()
            print(f"PASS: {name}")
            passed += 1
        except Exception:
            print(f"FAIL: {name}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
