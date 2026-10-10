"""定向实验：对已有账号做一次「新鲜登录」并立刻绑定 2FA。

官方在 enroll 时返回 `recent_auth_required`（必须最近重新认证过），
所以 2FA 只能在刚登录/刚注册后的新鲜会话里绑定。本脚本就复现这个时序：

  打开浏览器 → 提交邮箱 → 等真实验证码 → 输入 → 立刻绑定 2FA → 采集真实权益

用法::

    python scripts/live_fresh_login_2fa.py --email you@icloud.com \
        --inbox "https://icloud-api.top/s/xxx/you@icloud.com" --proxy 127.0.0.1:7897
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
    parser = argparse.ArgumentParser(description="新鲜登录 + 真实 2FA 绑定实验")
    parser.add_argument("--email", required=True)
    parser.add_argument("--inbox", required=True)
    parser.add_argument("--provider", default="icloud")
    parser.add_argument("--proxy", default="127.0.0.1:7897")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    from app import email_providers
    from app.browser import (
        _click_and_confirm_submit, _find_email_submit_button, _wait_for_post_email_step,
        create_driver, enter_verification_code,
    )
    from app.icloud_service import configure_icloud_account
    from app.two_factor_service import enable_account_2fa

    if args.provider == "icloud":
        configure_icloud_account(args.email, args.inbox)

    proxy = None
    if args.proxy and args.proxy.lower() not in ("off", "none"):
        host, _, port = args.proxy.partition(":")
        proxy = {"enabled": True, "type": "http", "host": host, "port": int(port or 7897),
                 "use_auth": False}

    initial_codes = set()
    try:
        initial_codes = set(email_providers.list_verification_codes(args.provider, args.inbox))
    except Exception:
        pass
    print(f"📬 收件箱已有验证码快照: {initial_codes or '无'}")

    driver = create_driver(headless=args.headless, proxy=proxy)
    try:
        driver.get("https://chatgpt.com")
        time.sleep(3)
        try:
            for button in driver.find_elements(By.XPATH, "//button[contains(., '登录') or contains(., 'Log in')]"):
                if button.is_displayed():
                    driver.execute_script("arguments[0].click();", button)
                    time.sleep(2)
                    break
        except Exception:
            pass

        email_input = WebDriverWait(driver, 20).until(
            EC.visibility_of_element_located((
                By.CSS_SELECTOR,
                'input[type="email"], input[name="email"], input[name="login_hint"], input[autocomplete="email"]',
            ))
        )
        driver.execute_script(
            """
            const el = arguments[0], value = arguments[1];
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            email_input, args.email,
        )
        time.sleep(0.6)
        submit = _find_email_submit_button(driver, email_input)
        if submit is not None:
            _click_and_confirm_submit(driver, submit, email_input)
        else:
            driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
        step = _wait_for_post_email_step(driver, timeout=45)
        print(f"🔀 邮箱提交后页面状态: {step}  (URL: {driver.current_url})")
        if step == "login_with":
            from app.browser import choose_email_code_login

            if choose_email_code_login(driver, log=print):
                step = _wait_for_post_email_step(driver, timeout=45)
                print(f"🔀 选择邮箱验证码后的页面状态: {step}  (URL: {driver.current_url})")
        if step in ("unknown",):
            return 1

        print("⏳ 等待真实验证码（最长 120s）...")
        code = email_providers.wait_for_verification_email(
            args.provider, args.inbox, timeout=120, exclude_codes=initial_codes,
        )
        if not code:
            print("❌ 未收到新的验证码")
            return 1
        print(f"✅ 收到验证码: {code}")
        enter_verification_code(driver, code)
        time.sleep(6)
        print(f"🔍 当前页面: {driver.current_url}")

        print("\n=== 新鲜会话下绑定 2FA（官方 recent_auth 窗口内）===")
        result = enable_account_2fa(driver, account_password="", log_func=print)
        print("\n=== 2FA 结果 ===")
        print(json.dumps({k: v for k, v in result.items()}, ensure_ascii=False, indent=2, default=str)[:3000])
        return 0 if result.get("success") else 1
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
