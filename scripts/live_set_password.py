"""用本地保存的真实 cookie 恢复会话后，尝试在官方设置页为免密账号设置真实密码。

用法::

    python scripts/live_set_password.py --email you@icloud.com --proxy 127.0.0.1:7897
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gpt-auto-register"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="免密账号真实设置密码")
    parser.add_argument("--email", required=True)
    parser.add_argument("--inbox", default="", help="收件箱链接（官方会要求邮箱验证码）")
    parser.add_argument("--provider", default="icloud")
    parser.add_argument("--proxy", default="127.0.0.1:7897")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--write", action="store_true", help="成功后写回账号库")
    args = parser.parse_args()

    from selenium.webdriver.common.by import By

    from app.account_perfector import _restore_saved_session, set_password_via_settings
    from app.browser import create_driver, dump_page_diagnostics
    from app.config import cfg, PROJECT_ROOT
    from app.utils import generate_random_password, save_to_txt

    proxy = None
    if args.proxy and args.proxy.lower() not in ("off", "none"):
        host, _, port = args.proxy.partition(":")
        proxy = {"enabled": True, "type": "http", "host": host, "port": int(port or 7897),
                 "use_auth": False}

    new_password = generate_random_password()
    driver = create_driver(headless=args.headless, proxy=proxy)
    try:
        if not _restore_saved_session(driver, args.email, print):
            print("❌ 无法用本地 cookie 恢复会话")
            return 1
        ok = set_password_via_settings(driver, new_password, print,
                                       provider=args.provider,
                                       inbox_url=args.inbox or None)
        print(f"\n🏁 设置密码结果: {'成功' if ok else '未确认'}")
        if ok:
            print(f"   邮箱: {args.email}")
            print(f"   密码: {new_password}")
            if args.write:
                save_to_txt(args.email, new_password, "已注册", provider="icloud")
                print("   💾 已写回账号库")
                print(json.dumps({"email": args.email, "password": new_password}, ensure_ascii=False))
        else:
            dump_page_diagnostics(driver, "set_password_final")
        return 0 if ok else 1
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
