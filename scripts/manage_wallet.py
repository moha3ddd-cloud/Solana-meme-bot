"""
CLI لإدارة الـWallet Keystore
================================
سكريبت تفاعلي — الاستخدام:

    python3 scripts/manage_wallet.py create        # يولّد wallet جديد
    python3 scripts/manage_wallet.py import         # يستورد private key موجود
    python3 scripts/manage_wallet.py show-pubkey     # يعرض العنوان العام بس

⚠️ لازم `pip install solders base58` الأول.
⚠️ الأوامر دي بتاخد الباسورد عبر `getpass` (مايظهرش وانت بتكتبه) —
   وممنوع تمريره كـargument في command line (بيفضل في shell history).
"""

from __future__ import annotations

import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DEFAULT_KEYSTORE_PATH = "data/wallet_keystore.json"


def cmd_create():
    try:
        from solders.keypair import Keypair
    except ImportError:
        print("محتاج solders: pip install solders base58")
        return

    from core.wallet.keystore import create_keystore

    path = input(f"مسار الـkeystore [{DEFAULT_KEYSTORE_PATH}]: ").strip() or DEFAULT_KEYSTORE_PATH
    if os.path.exists(path):
        print(f"في ملف موجود بالفعل في {path} — امسحه الأول لو عايز تستبدله.")
        return

    password = getpass.getpass("باسورد جديد للـkeystore (8 حروف على الأقل): ")
    password_confirm = getpass.getpass("أكّد الباسورد: ")
    if password != password_confirm:
        print("الباسورد مش متطابق.")
        return

    keypair = Keypair()
    create_keystore(bytes(keypair), password, path)

    print(f"\n✅ Wallet جديد اتعمل وخُزّن (مشفّر) في {path}")
    print(f"العنوان العام (Public Key): {keypair.pubkey()}")
    print("\n⚠️ ده wallet جديد فاضي — لازم تحوّل SOL له قبل أي استخدام.")
    print("⚠️ احتفظ بنسخة احتياطية من الباسورد في مكان آمن منفصل عن الملف نفسه.")


def cmd_import():
    try:
        from solders.keypair import Keypair
    except ImportError:
        print("محتاج solders: pip install solders base58")
        return

    from core.wallet.keystore import create_keystore

    path = input(f"مسار الـkeystore [{DEFAULT_KEYSTORE_PATH}]: ").strip() or DEFAULT_KEYSTORE_PATH
    if os.path.exists(path):
        print(f"في ملف موجود بالفعل في {path} — امسحه الأول لو عايز تستبدله.")
        return

    private_key_input = getpass.getpass(
        "الـprivate key الحالي (base58 من Phantom/Solflare، مايظهرش وانت بتكتبه): "
    )
    try:
        keypair = Keypair.from_base58_string(private_key_input.strip())
    except Exception:
        print("الصيغة غلط — لازم تكون base58 string.")
        return
    finally:
        del private_key_input

    password = getpass.getpass("باسورد جديد للـkeystore (8 حروف على الأقل): ")
    password_confirm = getpass.getpass("أكّد الباسورد: ")
    if password != password_confirm:
        print("الباسورد مش متطابق.")
        return

    create_keystore(bytes(keypair), password, path)
    print(f"\n✅ اتخزن (مشفّر) في {path}")
    print(f"العنوان العام (Public Key): {keypair.pubkey()}")


def cmd_show_pubkey():
    try:
        from solders.keypair import Keypair
    except ImportError:
        print("محتاج solders: pip install solders base58")
        return

    from core.wallet.keystore import WrongPasswordError, load_keystore

    path = input(f"مسار الـkeystore [{DEFAULT_KEYSTORE_PATH}]: ").strip() or DEFAULT_KEYSTORE_PATH
    password = getpass.getpass("الباسورد: ")

    try:
        secret = load_keystore(password, path)
    except WrongPasswordError:
        print("باسورد غلط.")
        return

    keypair = Keypair.from_bytes(secret)
    print(f"Public Key: {keypair.pubkey()}")


COMMANDS = {"create": cmd_create, "import": cmd_import, "show-pubkey": cmd_show_pubkey}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(f"استخدام: python3 {sys.argv[0]} [{'|'.join(COMMANDS)}]")
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
