"""定向实验：找出官方 MFA (TOTP) 的 enroll / activate 真实端点与报文。

它会：用本地保存的真实 cookie 打开浏览器恢复登录 → 依次调用候选端点 →
把每个端点返回的**完整 HTTP 状态与响应体**打印出来，用于确定真实的激活路径。

用法::

    python scripts/probe_mfa_api.py --email you@example.com --proxy 127.0.0.1:7897
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

CHAT = "https://chatgpt.com"
AUTH = "https://auth.openai.com"


def main() -> int:
    parser = argparse.ArgumentParser(description="探测官方 MFA 端点真实报文")
    parser.add_argument("--email", required=True)
    parser.add_argument("--proxy", default="127.0.0.1:7897")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--code", default="", help="已拿到密钥时，用它计算验证码（可选）")
    args = parser.parse_args()

    from app.account_perfector import _restore_saved_session
    from app.browser import create_driver
    from app.two_factor_service import (
        browser_fetch, generate_totp_code, get_page_access_token, read_mfa_status,
    )

    proxy = None
    if args.proxy and args.proxy.lower() not in ("off", "none"):
        host, _, port = args.proxy.partition(":")
        proxy = {"enabled": True, "type": "http", "host": host, "port": int(port or 7897),
                 "use_auth": False}

    driver = create_driver(headless=args.headless, proxy=proxy)
    try:
        if not _restore_saved_session(driver, args.email, print):
            print("❌ 无法用本地 cookie 恢复会话，请先跑一次注册或完善流程")
            return 1
        driver.get(f"{CHAT}/#settings/Security")
        time.sleep(3)
        driver.get(CHAT)
        time.sleep(2)

        bearer = get_page_access_token(driver)
        print(f"🔑 access token: {'已获取' if bearer else '未获取'}")

        print("\n=== 当前 2FA 状态 ===")
        status = read_mfa_status(driver)
        print(json.dumps(status, ensure_ascii=False, indent=2, default=str)[:2000])

        print("\n=== MFA 相关端点探测（同源 + Bearer）===")
        probes = [
            ("GET", f"{CHAT}/backend-api/accounts/mfa"),
            ("GET", f"{CHAT}/backend-api/accounts/mfa/factors"),
            ("POST", f"{CHAT}/backend-api/accounts/mfa/enroll", {"factor_type": "totp"}),
            ("POST", f"{CHAT}/backend-api/accounts/mfa/enroll/totp", {"factor_type": "totp"}),
        ]
        secret = ""
        for method, url, *rest in probes:
            body = rest[0] if rest else None
            result = browser_fetch(driver, method, url, body, bearer=bearer)
            print(f"\n--- {method} {url} -> HTTP {result.get('status')} "
                  f"{result.get('error', '')}")
            print((result.get("text") or "")[:1500])
            if result.get("status") == 200 and body:
                try:
                    payload = json.loads(result["text"])
                except (ValueError, TypeError):
                    payload = None
                from app.two_factor_service import _parse_secret

                found = _parse_secret(payload)
                if found:
                    secret = found
                    print(f"🔑 捕获密钥: {secret}")
                    break

        if secret:
            code = generate_totp_code(secret)
            print(f"\n🔢 由密钥计算出的当前验证码: {code}")
            print("\n=== 激活端点探测 ===")
            activate_probes = [
                ("POST", f"{CHAT}/backend-api/accounts/mfa/activate", {"code": code, "factor_type": "totp"}),
                ("POST", f"{CHAT}/backend-api/accounts/mfa/enroll/verify", {"code": code}),
                ("POST", f"{CHAT}/backend-api/accounts/mfa/verify", {"code": code}),
                ("POST", f"{CHAT}/backend-api/accounts/mfa/factors/verify", {"code": code}),
                ("PUT", f"{CHAT}/backend-api/accounts/mfa", {"code": code, "factor_type": "totp"}),
                ("POST", f"{CHAT}/backend-api/accounts/mfa/factors", {"code": code, "factor_type": "totp"}),
            ]
            for method, url, body in activate_probes:
                result = browser_fetch(driver, method, url, body, bearer=bearer)
                print(f"\n--- {method} {url} -> HTTP {result.get('status')} {result.get('error', '')}")
                print((result.get("text") or "")[:800])

            print("\n=== 激活后状态复核 ===")
            print(json.dumps(read_mfa_status(driver), ensure_ascii=False, indent=2, default=str)[:1500])

        print("\n=== 安全设置页现场 ===")
        driver.get(f"{CHAT}/#settings/Security")
        time.sleep(4)
        try:
            from selenium.webdriver.common.by import By

            body_text = driver.find_element(By.TAG_NAME, "body").text or ""
            print(body_text[:1500])
            buttons = [
                (b.text or "").strip() for b in driver.find_elements(By.CSS_SELECTOR, "button")
                if b.is_displayed()
            ]
            print("可见按钮:", [b for b in buttons if b][:30])
        except Exception as exc:  # noqa: BLE001
            print(f"读取页面失败: {exc}")
        return 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
