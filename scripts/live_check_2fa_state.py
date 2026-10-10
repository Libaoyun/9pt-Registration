"""决定性检查：用真实密钥的账号登录后，看官方设置页里 MFA 到底是不是"已启用"。

用途：回答"通过 enroll 拿到的 32 位密钥，官方是否真的认它为生效的 2FA"。

用法::
    python scripts/live_check_2fa_state.py --email you@icloud.com --inbox "https://..." --proxy 127.0.0.1:7897
"""

from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(description="检查账号在官方的 MFA 真实状态")
    parser.add_argument("--email", required=True)
    parser.add_argument("--inbox", default="")
    parser.add_argument("--provider", default="icloud")
    parser.add_argument("--proxy", default="127.0.0.1:7897")
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()

    from selenium.webdriver.common.by import By

    from app.account_perfector import _restore_saved_session, fresh_login_via_email_code
    from app.browser import create_driver, dump_page_diagnostics
    from app.config import cfg, PROJECT_ROOT
    from app.stored_accounts import load_accounts_from_file

    path = Path(cfg.files.accounts_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    record = next((r for r in load_accounts_from_file(str(path)) if r["email"] == args.email), {})
    inbox = args.inbox or (record.get("mailbox_credential") or "").strip()
    key = (record.get("two_factor_secret") or "").strip()
    print(f"📖 本地记录的 32 位密钥: {key or '（无）'}")

    proxy = None
    if args.proxy and args.proxy.lower() not in ("off", "none"):
        host, _, port = args.proxy.partition(":")
        proxy = {"enabled": True, "type": "http", "host": host, "port": int(port or 7897),
                 "use_auth": False}

    driver = create_driver(headless=args.headless, proxy=proxy)
    try:
        logged_in = _restore_saved_session(driver, args.email, print)
        if not logged_in:
            print("  ↪️ cookie 失效，改用邮箱验证码登录 ...")
            logged_in = fresh_login_via_email_code(driver, args.email, args.provider, inbox, print)
        if not logged_in:
            print("❌ 无法登录，无法检查 MFA 状态")
            return 1

        print("\n🔍 打开安全设置页检查 MFA 真实状态 ...")
        from app.browser import open_security_settings

        if not open_security_settings(driver):
            print("⚠️ 设置页未能渲染，尝试直接读主页兜底")
        time.sleep(3)
        text = ""
        try:
            text = driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            pass
        dump_page_diagnostics(driver, "mfa_state_check")
        print(f"\n当前页面: {driver.current_url}")
        print("\n=== 安全设置页正文 ===\n" + text[:2500])

        enabled_markers = ("关闭多重验证", "关闭", "移除", "Turn off", "Remove", "已开启", "备份码")
        has_mfa_block = "多因素" in text or "MFA" in text or "Authenticator" in text
        looks_enabled = any(m in text for m in enabled_markers) and has_mfa_block

        print("\n" + "=" * 70)
        print(f"MFA 区块是否存在: {has_mfa_block}")
        print(f"是否显示为已启用: {'✅ 是' if looks_enabled else '❓ 未能确认（可能未启用）'}")
        print("=" * 70)
        print(f"结论：本地密钥 {key or '（无）'} "
              f"{'已生效（官方页面显示已启用）' if looks_enabled else '未能在官方页面确认为已启用'}")
        return 0 if looks_enabled else 2
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
