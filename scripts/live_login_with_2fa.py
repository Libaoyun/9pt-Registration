"""决定性测试：用已保存的 32 位密钥去真实登录，验证密钥到底能不能用。

流程：提交邮箱 → 若进入"选择登录方式"页则选邮箱验证码 → 输入真实验证码
     → 观察官方是否弹出 2FA 验证码输入框
        · 弹出且用我们密钥算出的 TOTP 通过 → 密钥有效（enroll 即已生效）
        · 未弹出 → 说明该密钥尚未在官方激活，需要在设置页手动启用

用法::

    python scripts/live_login_with_2fa.py --email you@icloud.com --inbox "https://..." \
        [--secret KLCLX36I5XPZ6BIIDBKMBY3YHFCV7ZKU] --proxy 127.0.0.1:7897
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
    parser = argparse.ArgumentParser(description="用 32 位密钥真实登录验证")
    parser.add_argument("--email", required=True)
    parser.add_argument("--inbox", required=True)
    parser.add_argument("--provider", default="icloud")
    parser.add_argument("--secret", default="", help="32 位 TOTP 密钥（缺省时从账号库读取）")
    parser.add_argument("--proxy", default="127.0.0.1:7897")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    from app import email_providers
    from app.browser import (
        _click_and_confirm_submit, _find_email_submit_button, _wait_for_post_email_step,
        choose_email_code_login, create_driver, dump_page_diagnostics, enter_verification_code,
    )
    from app.icloud_service import configure_icloud_account
    from app.two_factor_service import generate_totp_code

    secret = args.secret.strip()
    if not secret:
        from app.config import cfg, PROJECT_ROOT
        from app.stored_accounts import get_registered_account_by_email

        path = Path(cfg.files.accounts_file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        record = get_registered_account_by_email(str(path), args.email) or {}
        secret = (record.get("two_factor_secret") or "").strip()
        print(f"📖 从账号库读取密钥: {secret or '（无）'}")

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

    driver = create_driver(headless=args.headless, proxy=proxy)
    try:
        driver.get("https://chatgpt.com")
        time.sleep(3)
        for button in driver.find_elements(By.XPATH, "//button[contains(., '登录') or contains(., 'Log in')]"):
            if button.is_displayed():
                driver.execute_script("arguments[0].click();", button)
                time.sleep(2)
                break

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
        print(f"🔀 第一步页面状态: {step} | URL: {driver.current_url}")
        if step == "login_with":
            choose_email_code_login(driver, log=print)
            step = _wait_for_post_email_step(driver, timeout=45)
            print(f"🔀 选择邮箱验证码后: {step} | URL: {driver.current_url}")

        if step != "verification":
            print("ℹ️ 未进入验证码页，尝试直接等待 2FA 输入框出现...")

        # 需要验证码时拉取
        code = email_providers.wait_for_verification_email(
            args.provider, args.inbox, timeout=120, exclude_codes=initial_codes,
        )
        if code:
            print(f"✅ 收到邮箱验证码: {code}")
            enter_verification_code(driver, code)
            time.sleep(6)
        else:
            print("ℹ️ 未收到新的邮箱验证码（可能不需要）")

        print(f"🔍 提交验证码后页面: {driver.current_url}")

        # 关键判断：是否出现 2FA 一次性验证码输入框
        def _totp_inputs():
            found = []
            for selector in ('input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]',
                             'input[name="code"]', 'input[maxlength="1"]'):
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if element.is_displayed():
                            found.append(element)
                    except Exception:
                        continue
            return found

        inputs = _totp_inputs()
        page_text = ""
        try:
            page_text = (driver.find_element(By.TAG_NAME, "body").text or "")[:1200]
        except Exception:
            page_text = ""
        looks_like_2fa = any(marker in page_text for marker in
                             ("验证器", "authenticator", "两步", "2FA", "一次性验证码", "authentication code"))

        if not inputs and not looks_like_2fa:
            print("\n" + "=" * 70)
            print("❌ 登录流程没有要求 2FA 验证码 → 该账号的 2FA 因子尚未激活")
            print("   结论：enroll 拿到的密钥当前不可用于登录；需要在官方设置页手动启用 MFA。")
            print("=" * 70)
            dump_page_diagnostics(driver, "login_no_2fa_prompt")
            return 2

        print("\n" + "=" * 70)
        print("✅ 官方登录要求 2FA 验证码 → 因子已生效！现在用保存的密钥计算 TOTP 验证")
        print("=" * 70)
        if not secret:
            print("⚠️ 本地没有密钥，无法继续验证")
            return 3
        totp = generate_totp_code(secret)
        print(f"🔢 密钥 {secret} 当前动态码: {totp}")
        if len(inputs) >= 6:
            for index, digit in enumerate(totp):
                inputs[index].send_keys(digit)
        elif inputs:
            inputs[0].click()
            inputs[0].send_keys(totp)
        time.sleep(1)
        for element in driver.find_elements(
            By.XPATH, "//button[@type='submit' or contains(., '继续') or contains(., 'Continue') or contains(., '验证')]"
        ):
            if element.is_displayed() and element.is_enabled():
                driver.execute_script("arguments[0].click();", element)
                break
        time.sleep(6)
        try:
            after = (driver.find_element(By.TAG_NAME, "body").text or "")[:800]
        except Exception:
            after = ""
        success_markers = ("有什么可以帮你", "有什么我能帮", "今天有什么", "ChatGPT", "新聊天")
        if any(marker in after for marker in success_markers) or "chatgpt.com" in driver.current_url:
            print("\n🎉 用保存的 32 位密钥成功通过 2FA 登录 → 密钥真实有效，可直接使用")
            return 0
        print("\n⚠️ 提交 2FA 后未确认登录成功，现场已保存")
        dump_page_diagnostics(driver, "login_2fa_result")
        return 4
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
