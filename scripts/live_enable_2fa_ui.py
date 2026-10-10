"""按官方 UI 真实启用 MFA 并取回"官方认定的生效密钥"。

流程（全部真实）：
  1) 真实登录（邮箱验证码；已有账号会进入 /auth/login_with，其实就是收件箱验证码页）
  2) 进入 chatgpt.com/#settings/Security
  3) 点击「多因素身份验证 (MFA) → Authenticator app / 添加」
  4) 从官方对话框抓取 32 位密钥（二维码/明文密钥）
  5) 用该密钥算 TOTP 填入并提交 → 官方确认后打印密钥

用法::

    python scripts/live_enable_2fa_ui.py --email you@icloud.com --inbox "https://..." \
        --proxy 127.0.0.1:7897 [--write]
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
    parser = argparse.ArgumentParser(description="官方 UI 启用 MFA 并取回生效密钥")
    parser.add_argument("--email", required=True)
    parser.add_argument("--inbox", required=True)
    parser.add_argument("--provider", default="icloud")
    parser.add_argument("--proxy", default="127.0.0.1:7897")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--write", action="store_true", help="成功后写回账号库")
    args = parser.parse_args()

    from selenium.webdriver.common.by import By

    from app import email_providers
    from app.browser import (
        _click_and_confirm_submit, _find_email_submit_button, _wait_for_post_email_step,
        choose_email_code_login, create_driver, dump_page_diagnostics,
        enter_verification_code, verify_logged_in,
    )
    from app.icloud_service import configure_icloud_account
    from app.two_factor_service import (
        extract_secret_from_page, generate_totp_code, read_mfa_status,
    )

    if args.provider == "icloud":
        configure_icloud_account(args.email, args.inbox)

    proxy = None
    if args.proxy and args.proxy.lower() not in ("off", "none"):
        host, _, port = args.proxy.partition(":")
        proxy = {"enabled": True, "type": "http", "host": host, "port": int(port or 7897),
                 "use_auth": False}

    def _text() -> str:
        try:
            return (driver.find_element(By.TAG_NAME, "body").text or "").strip()
        except Exception:
            return ""

    initial_codes = set()
    try:
        initial_codes = set(email_providers.list_verification_codes(args.provider, args.inbox))
    except Exception:
        pass

    driver = create_driver(headless=args.headless, proxy=proxy)
    try:
        # ---------- 1) 真实登录 ----------
        driver.get("https://chatgpt.com")
        time.sleep(3)
        for button in driver.find_elements(By.XPATH, "//button[contains(., '登录') or contains(., 'Log in')]"):
            if button.is_displayed():
                driver.execute_script("arguments[0].click();", button)
                time.sleep(2)
                break

        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        email_input = WebDriverWait(driver, 25).until(
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
        print(f"🔀 第一步页面状态: {step}")
        if step == "login_with":
            # 该页已是"输入邮箱验证码"页；必要时点一次发送
            print(f"  📄 页面正文: {_text()[:120]}")
            choose_email_code_login(driver, log=print)
            step = _wait_for_post_email_step(driver, timeout=30)
            print(f"🔀 选择邮箱验证码后: {step}")

        code = email_providers.wait_for_verification_email(
            args.provider, args.inbox, timeout=120, exclude_codes=initial_codes,
        )
        if code:
            print(f"✅ 邮箱验证码: {code}")
            enter_verification_code(driver, code)
            time.sleep(6)
        if not verify_logged_in(driver, timeout=45):
            print("❌ 登录未完成")
            dump_page_diagnostics(driver, "ui_mfa_login_failed")
            return 1
        print("✅ 已登录")

        # ---------- 2) 打开安全设置 ----------
        driver.get("https://chatgpt.com/#settings/Security")
        time.sleep(5)
        print(f"\n=== 安全设置页正文 ===\n{_text()[:1200]}")

        status = read_mfa_status(driver)
        print(f"\n当前 MFA 状态: {status.get('enabled')}")

        # ---------- 3) 进入 MFA 添加流程 ----------
        clicked = False
        click_specs = (
            "//*[contains(normalize-space(.), 'Authenticator') or contains(normalize-space(.), '身份验证器')]",
            "//button[contains(normalize-space(.), '添加') or contains(normalize-space(.), 'Add') "
            "or contains(normalize-space(.), '设置') or contains(normalize-space(.), '开启') "
            "or contains(normalize-space(.), 'Set up') or contains(normalize-space(.), 'Turn on')]",
        )
        for xpath in click_specs:
            for element in driver.find_elements(By.XPATH, xpath):
                try:
                    if element.is_displayed() and element.is_enabled():
                        label = (element.text or "")[:40].replace("\n", " ")
                        driver.execute_script("arguments[0].click();", element)
                        print(f"  👉 已点击: {label!r}")
                        clicked = True
                        time.sleep(4)
                        break
                except Exception:
                    continue
            if clicked:
                break

        if not clicked:
            print("  ⚠️ 未找到 MFA 入口")
            dump_page_diagnostics(driver, "ui_mfa_no_entry")
            return 2

        print(f"\n=== 点击后页面正文 ===\n{_text()[:1200]}")
        dump_page_diagnostics(driver, "ui_mfa_dialog")

        # ---------- 4) 抓密钥 ----------
        secret = extract_secret_from_page(driver)
        print(f"\n🔑 页面抓取到的密钥: {secret or '（未抓到）'}")
        if not secret:
            print("  ℹ️ 未能从页面抓到密钥；诊断已保存（可据此继续定位二维码/明文密钥位置）")
            return 3

        # ---------- 5) 提交 TOTP 完成激活 ----------
        totp = generate_totp_code(secret)
        print(f"🔢 当前动态码: {totp}")
        inputs = [
            element for element in driver.find_elements(
                By.CSS_SELECTOR,
                'input[autocomplete="one-time-code"], input[inputmode="numeric"], input[maxlength="1"], input[type="text"]',
            ) if element.is_displayed()
        ]
        if len(inputs) >= 6:
            for index, digit in enumerate(totp):
                inputs[index].send_keys(digit)
        elif inputs:
            inputs[0].click()
            inputs[0].send_keys(totp)
        else:
            print("  ⚠️ 未找到验证码输入框")
            return 4
        time.sleep(1)
        for element in driver.find_elements(
            By.XPATH, "//button[@type='submit' or contains(normalize-space(.), '继续') "
            "or contains(normalize-space(.), '验证') or contains(normalize-space(.), 'Verify') "
            "or contains(normalize-space(.), 'Continue') or contains(normalize-space(.), '启用')]"
        ):
            if element.is_displayed() and element.is_enabled():
                driver.execute_script("arguments[0].click();", element)
                break
        time.sleep(5)

        print(f"\n=== 提交后页面正文 ===\n{_text()[:1200]}")
        after = read_mfa_status(driver)
        page_text = _text()
        enabled_markers = ("关闭", "移除", "Turn off", "已开启", "Remove", "备份码", "backup")
        confirmed = after.get("enabled") is True or any(m in page_text for m in enabled_markers)
        print(f"\n🏁 MFA 结果: {'✅ 官方确认已启用' if confirmed else '⚠️ 未确认'}")
        print(f"   32位密钥: {secret}")
        if confirmed and args.write:
            from app.config import cfg, PROJECT_ROOT
            from app.stored_accounts import load_accounts_from_file, update_account_check_result_in_file

            path = Path(cfg.files.accounts_file)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            rec = next((r for r in load_accounts_from_file(str(path)) if r["email"] == args.email), {})
            ev = dict(rec.get("profile_evidence") or {})
            ev["two_factor"] = {"bound": True, "activated": True, "verified_by": "official_ui",
                                "note": "通过官方设置页 Authenticator app 流程启用并确认"}
            update_account_check_result_in_file(
                str(path), args.email, rec.get("plan", "未检测"), rec.get("trial_status", "待官方确认"),
                quota=rec.get("quota"), expires_at=rec.get("expires_at"),
                account_status=rec.get("account_status"), two_factor_secret=secret, evidence=ev,
            )
            print("   💾 已写回账号库")
        print(json.dumps({"email": args.email, "two_factor_secret": secret, "activated": confirmed},
                         ensure_ascii=False))
        return 0 if confirmed else 5
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
