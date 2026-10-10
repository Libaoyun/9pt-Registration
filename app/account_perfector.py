"""ChatGPT 账号自动化完善服务。

职责（全部走官方真实通道，不伪造任何字段）:
  - 自动登录（密码分支 / 邮箱验证码分支），必要时通过官方邮件链接真实设置密码
  - 捕获真实 Web Session Token (``/api/auth/session`` 的 accessToken + session cookie) 并落盘
  - 通过官方 backend-api 采集真实的订阅计划、是否 Plus、1 个月试用资格、到期时间
  - 通过官方 MFA 接口 / 真实 UI 流程开启 2FA 并取回真实 Base32 密钥（官方确认后才写入）
  - 把完整证据写入 ``data/tokens/state-<email>.json``，账号 TXT 只写紧凑摘要
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from .account_checker import state_evidence_summary, save_state_evidence
from .account_probe import probe_in_browser, probe_with_token
from .browser import create_driver, type_slowly, enter_verification_code
from .config import cfg, PROJECT_ROOT
from .stored_accounts import load_accounts_from_file, update_account_check_result_in_file
from .two_factor_service import enable_account_2fa
from .utils import generate_random_password, save_to_txt
from . import email_providers

_TZ = timezone(timedelta(hours=8))

_SESSION_JS = r"""
return (async () => {
  try {
    const resp = await fetch('/api/auth/session', {
      credentials: 'include',
      headers: { 'accept': 'application/json' },
    });
    const text = await resp.text();
    let payload = null;
    try { payload = JSON.parse(text); } catch (e) { payload = null; }
    return { status: resp.status, payload: payload };
  } catch (err) {
    return { status: 0, error: String(err), payload: null };
  }
})()
"""


def _resolve_accounts_file() -> str:
    path = Path(cfg.files.accounts_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path)


def _autodetect_proxy(log_func: Callable[[str], None]) -> dict | None:
    """本机常见代理端口探测（可用 GPT_NO_PROXY_AUTODETECT=1 关闭）。"""
    if os.environ.get("GPT_NO_PROXY_AUTODETECT") == "1":
        return None
    import socket

    for port in (7897, 7890, 10809, 10808, 8080):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                log_func(f"  🌐 自动检测到本机代理 127.0.0.1:{port}，已用于本次在线操作")
                return {"enabled": True, "type": "http", "host": "127.0.0.1", "port": port, "use_auth": False}
        except Exception:
            continue
    return None


def _capture_session(driver, email: str, log_func: Callable[[str], None]) -> dict:
    """抓取真实 Web 凭据并落盘到 ``data/tokens/web-<email>.json``。"""
    info = {"access_token": "", "payload": None, "cookie": ""}
    try:
        raw = driver.execute_script(_SESSION_JS)
        if isinstance(raw, dict):
            payload = raw.get("payload")
            if isinstance(payload, dict):
                info["payload"] = payload
                info["access_token"] = payload.get("accessToken") or ""
            log_func(f"  🌐 /api/auth/session -> HTTP {raw.get('status')}")
    except Exception as exc:  # noqa: BLE001
        log_func(f"  ⚠️ 读取会话接口失败: {exc}")

    try:
        cookies = driver.get_cookies() or []
    except Exception:
        cookies = []
    session_cookie_names = [
        cookie.get("name", "") for cookie in cookies
        if "session-token" in (cookie.get("name") or "").lower()
    ]
    if session_cookie_names:
        for cookie in cookies:
            if cookie.get("name") == session_cookie_names[0]:
                info["cookie"] = cookie.get("value", "")
                break

    if info["access_token"] or info["cookie"] or cookies:
        try:
            token_dir = Path(cfg.oauth.token_json_dir)
            if not token_dir.is_absolute():
                token_dir = PROJECT_ROOT / token_dir
            token_dir.mkdir(parents=True, exist_ok=True)
            target = token_dir / f"web-{email}.json"
            target.write_text(
                json.dumps(
                    {
                        "email": email,
                        "type": "web",
                        "access_token": info["access_token"],
                        "session_token": info["cookie"],
                        "session_cookie_names": session_cookie_names,
                        "cookies": cookies,
                        "expires": (info["payload"] or {}).get("expires", ""),
                        "account": (info["payload"] or {}).get("account"),
                        "user": (info["payload"] or {}).get("user"),
                        "captured_at": datetime.now(_TZ).isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            info["cookies"] = cookies
            log_func(f"  💾 真实 Web 凭据已保存: {target.name}（cookie {len(cookies)} 条）")
        except Exception as exc:  # noqa: BLE001
            log_func(f"  ⚠️ 保存 Web 凭据失败: {exc}")
    return info


def _collect_state(driver, email: str, session_info: dict, proxy, log_func) -> dict:
    state = probe_in_browser(driver, email=email, log=log_func)
    if not state.get("verified") and session_info.get("access_token"):
        log_func("  ℹ️ 内页通道未取回权益，回退 Bearer 协议通道复核...")
        fallback = probe_with_token(
            session_info["access_token"], email=email, token_kind="web", proxy=proxy, timeout=12
        )
        if fallback.get("verified"):
            fallback["evidence"]["fallback_from"] = "browser_probe"
            return fallback
    return state


def _extract_reset_link(text: str) -> str:
    """从邮件正文里提取官方重置密码链接。"""
    if not text:
        return ""
    patterns = (
        r"https://auth\.openai\.com[^\s\"'<>]*(?:reset-password|ticket=)[^\s\"'<>]*",
        r"https://[^\s\"'<>]*openai\.com[^\s\"'<>]*ticket=[^\s\"'<>]*",
        r"https://chatgpt\.com[^\s\"'<>]*reset[^\s\"'<>]*",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0).replace("&amp;", "&")
    return ""


def set_password_via_settings(
    driver,
    new_password: str,
    log_func: Callable[[str], None] = print,
    provider: str | None = None,
    inbox_url: str | None = None,
) -> bool:
    """在官方安全设置页用「密码 → 添加」为免密账号设置真实密码。

    实测流程（2026-10，登录态）::

        chatgpt.com/#settings/Security
          账户安全与登录 → 密码 [添加]  ← 点击后跳转
        https://auth.openai.com/email-verification  ← 官方要求邮箱验证码
          → 输入收件箱里的 6 位验证码 → 进入设置新密码表单 → 提交
    """
    from .browser import _set_input_value, dump_page_diagnostics, enter_verification_code

    def _log(message: str) -> None:
        log_func(message)

    try:
        driver.get("https://chatgpt.com/#settings/Security")
        time.sleep(4)
        add_button = None
        for element in driver.find_elements(By.XPATH, "//button[contains(normalize-space(.), '添加')]"):
            try:
                if not (element.is_displayed() and element.is_enabled()):
                    continue
            except Exception:
                continue
            try:
                container = element.find_element(
                    By.XPATH, "ancestor::*[.//*[contains(normalize-space(.), '密码')]][1]"
                )
                if "密码" in (container.text or ""):
                    add_button = element
                    break
            except Exception:
                continue
        if add_button is None:
            visible = [
                element for element in driver.find_elements(By.XPATH, "//button[contains(normalize-space(.), '添加')]")
                if element.is_displayed()
            ]
            add_button = visible[0] if visible else None
        if add_button is None:
            _log("  ℹ️ 未找到密码区「添加」按钮")
            dump_page_diagnostics(driver, "password_add_button_missing")
            return False

        driver.execute_script("arguments[0].click();", add_button)
        time.sleep(3)
        _log(f"  👉 已点击密码区「添加」，当前页面: {driver.current_url}")

        # 官方可能要求邮箱验证码（实测会跳到 /email-verification）
        if "email-verification" in (driver.current_url or "") or "verification" in (driver.current_url or ""):
            _log("  📩 官方要求邮箱验证码以确认身份，正在从收件箱取码...")
            if not (provider and inbox_url):
                _log("  ⚠️ 缺少收件箱凭据，无法自动完成验证码确认")
                return False
            from . import email_providers

            code = None
            try:
                known = set(email_providers.list_verification_codes(provider, inbox_url))
            except Exception:
                known = set()
            deadline = time.time() + 90
            while time.time() < deadline and not code:
                time.sleep(3)
                try:
                    codes = [c for c in email_providers.list_verification_codes(provider, inbox_url)
                             if c and c not in known]
                except Exception:
                    codes = []
                if codes:
                    code = codes[0]
            if not code:
                _log("  ⚠️ 未收到用于设置密码的验证码")
                dump_page_diagnostics(driver, "password_email_verification_timeout")
                return False
            _log(f"  ✅ 收到验证码: {code}")
            enter_verification_code(driver, code)
            time.sleep(5)

        # 填写新密码并提交
        submitted = False
        for attempt in range(1, 4):
            fields = [
                element for element in driver.find_elements(
                    By.CSS_SELECTOR,
                    'input[type="password"], input[autocomplete="new-password"], input[name="password"]',
                ) if element.is_displayed()
            ]
            if fields:
                for element in fields[:2]:
                    _set_input_value(driver, element, new_password)
                    time.sleep(0.4)
                for element in driver.find_elements(
                    By.XPATH,
                    "//button[@type='submit' or contains(normalize-space(.), '继续') "
                    "or contains(normalize-space(.), '保存') or contains(normalize-space(.), '设置') "
                    "or contains(normalize-space(.), 'Continue') or contains(normalize-space(.), 'Save')]",
                ):
                    if element.is_displayed() and element.is_enabled():
                        driver.execute_script("arguments[0].click();", element)
                        submitted = True
                        break
                time.sleep(4)
            try:
                page_text = (driver.find_element(By.TAG_NAME, "body").text or "")[:2000]
            except Exception:
                page_text = ""
            if any(marker in page_text for marker in ("密码已", "已设置", "password updated", "修改密码", "更改密码", "Password updated")):
                _log("  ✅ 官方页面确认密码已设置")
                return True
            if submitted and attempt < 3:
                _log(f"  ↻ 第 {attempt} 次提交后未确认，重试...")
                time.sleep(2)
            elif not fields and attempt < 3:
                time.sleep(3)

        _log("  ⚠️ 未能确认密码已生效，现场诊断已保存")
        dump_page_diagnostics(driver, "password_add_result")
        return False
    except Exception as exc:  # noqa: BLE001
        _log(f"  ⚠️ 设置页添加密码异常: {exc}")
        return False


def set_initial_password(
    driver,
    email: str,
    provider: str,
    inbox_url: str,
    known_codes: set | None = None,
    log_func: Callable[[str], None] = print,
) -> str | None:
    """用官方「忘记密码」流程给免密注册的账号设置一个真实可用的密码。

    返回真实设置成功的新密码；无法取得官方确认时返回 None（不编造密码）。
    """
    from .utils import generate_random_password

    if not inbox_url:
        log_func("  ℹ️ 本地缺少收件箱凭据，无法自动设置初始密码（保持未设置）")
        return None

    before_text = email_providers.fetch_message_text(provider, inbox_url) or ""
    if not before_text:
        log_func(f"  ℹ️ {provider} 未提供邮件正文读取能力，无法自动设置初始密码（保持未设置）")
        return None

    new_password = generate_random_password()
    log_func("  🔑 该账号免密注册，正在通过官方「忘记密码」流程设置真实密码...")
    try:
        driver.get("https://auth.openai.com/forgot-password")
        time.sleep(3)
    except Exception as exc:  # noqa: BLE001
        log_func(f"  ⚠️ 打开忘记密码页失败: {exc}")
        return None

    try:
        field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((
                By.CSS_SELECTOR,
                'input[name="email"], input[type="email"], input[name="login_hint"], #email-input',
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
            field, email,
        )
        time.sleep(0.5)
        try:
            driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
        except Exception:
            field.send_keys(Keys.ENTER)
        time.sleep(3)
    except Exception as exc:  # noqa: BLE001
        log_func(f"  ⚠️ 提交忘记密码邮箱失败: {exc}")
        return None

    reset_link = ""
    for _ in range(20):
        time.sleep(3)
        text = email_providers.fetch_message_text(provider, inbox_url) or ""
        link = _extract_reset_link(text)
        if link and link not in before_text:
            reset_link = link
            break
    if not reset_link:
        log_func("  ⚠️ 未在收件箱中找到新的重置密码链接（可能受官方邮件频控）")
        return None

    log_func("  🔗 已捕获官方重置链接，正在设置新密码...")
    try:
        driver.get(reset_link)
        time.sleep(4)
        password_fields = [
            element for element in driver.find_elements(
                By.CSS_SELECTOR, 'input[type="password"], input[name="password"], input[autocomplete="new-password"]'
            ) if element.is_displayed()
        ]
        if not password_fields:
            log_func("  ⚠️ 重置页面未出现密码输入框")
            return None
        for element in password_fields[:2]:
            type_slowly(element, new_password)
            time.sleep(0.3)
        try:
            driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
        except Exception:
            driver.switch_to.active_element.send_keys(Keys.ENTER)
        time.sleep(5)

        page_text = ""
        try:
            page_text = (driver.find_element(By.TAG_NAME, "body").text or "")[:2000].lower()
        except Exception:
            pass
        success_markers = ("password updated", "密码已更新", "you're all set", "已设置", "signed in", "已登录")
        if any(marker in page_text for marker in success_markers) or "chatgpt.com" in (driver.current_url or ""):
            log_func(f"  ✅ 官方密码设置成功: {new_password}")
            return new_password
        log_func("  ⚠️ 未能确认密码已生效，不写入该密码")
        return None
    except Exception as exc:  # noqa: BLE001
        log_func(f"  ⚠️ 设置密码过程异常: {exc}")
        return None


def _restore_saved_session(driver, email: str, log_func: Callable[[str], None]) -> bool:
    """用本地保存的真实 cookie 恢复登录态（免邮箱验证码，最稳最快）。

    注册阶段已把全部 cookie 落盘到 ``data/tokens/web-<email>.json``；
    补全阶段直接把 cookie 塞回浏览器即可恢复会话（比再走一遍登录可靠得多）。
    """
    token_file = Path(cfg.oauth.token_json_dir)
    if not token_file.is_absolute():
        token_file = PROJECT_ROOT / token_file
    candidate = token_file / f"web-{email}.json"
    if not candidate.is_file():
        return False
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except Exception:
        return False

    saved_cookies = (payload or {}).get("cookies") or []
    if not saved_cookies:
        legacy = (payload or {}).get("session_token") or ""
        if legacy:
            saved_cookies = [{"name": "__Secure-next-auth.session-token", "value": legacy,
                              "domain": ".chatgpt.com", "path": "/"}]
    if not saved_cookies:
        log_func("  ℹ️ 本地未保存 cookie，改用账号登录流程")
        return False

    try:
        driver.get("https://chatgpt.com")
        time.sleep(2)
        added = 0
        for cookie in saved_cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            item = {
                "name": name,
                "value": value,
                "domain": cookie.get("domain") or ".chatgpt.com",
                "path": cookie.get("path") or "/",
            }
            for key in ("secure", "httpOnly", "sameSite"):
                if cookie.get(key) not in (None, ""):
                    item[key] = cookie[key]
            if cookie.get("expiry"):
                try:
                    item["expiry"] = int(cookie["expiry"])
                except (TypeError, ValueError):
                    pass
            try:
                driver.add_cookie(item)
                added += 1
            except Exception:
                continue
        log_func(f"  🍪 已注入本地保存的 cookie: {added}/{len(saved_cookies)} 条")
        # 用官方 session 接口判定是否真的恢复成功（不依赖前端渲染）；
        # 首次导航可能页面尚未就绪，这里重试几次。
        for attempt in range(1, 4):
            try:
                driver.get("https://chatgpt.com/api/auth/session")
            except Exception:
                time.sleep(2)
                continue
            time.sleep(2.5)
            session_json = ""
            try:
                session_json = (driver.find_element(By.TAG_NAME, "body").text or "").strip()
            except Exception:
                session_json = ""
            payload_json = {}
            if session_json.startswith("{"):
                try:
                    payload_json = json.loads(session_json)
                except (ValueError, TypeError):
                    payload_json = {}
            if isinstance(payload_json, dict) and payload_json.get("accessToken"):
                user_email = (payload_json.get("user") or {}).get("email")
                log_func(
                    f"  ✅ 已用本地真实 cookie 恢复登录态"
                    f"（/api/auth/session 返回 accessToken，会话账号: {user_email}）"
                )
                driver.get("https://chatgpt.com")
                time.sleep(3)
                return True
            log_func(f"  ⏳ 第 {attempt} 次会话校验未通过，重试...")
        log_func("  ℹ️ 本地 cookie 已失效（session 接口未返回 accessToken），回退到账号登录流程")
        driver.get("https://chatgpt.com")
        time.sleep(2)
        return False
    except Exception as exc:  # noqa: BLE001
        log_func(f"  ⚠️ 注入 cookie / 会话校验异常: {exc}")
        return False


def fresh_login_via_email_code(
    driver,
    email: str,
    provider: str,
    inbox_url: str,
    log_func: Callable[[str], None] = print,
    timeout: int = 45,
    password: str | None = None,
) -> bool:
    """真实登录已有账号（优先密码方式，回退邮箱验证码方式）。

    官方对 MFA enroll 有 `recent_auth_required` 限制，必须先做一次新鲜认证才能绑定 2FA，
    所以这是"给老账号补 2FA 密钥"的必经步骤。

    协议版注册的账号自带密码 → 登录页会提供「使用密码继续」，
    走密码登录可绕开邮箱验证码（官方对免密新号常常拒发验证码）。
    """
    from .browser import (
        _click_and_confirm_submit, _find_email_submit_button, _set_input_value,
        _wait_for_post_email_step, choose_email_code_login, enter_verification_code,
        verify_logged_in,
    )
    from . import email_providers

    if not inbox_url:
        log_func("  ⚠️ 缺少收件箱凭据，无法做新鲜登录")
        return False

    initial_codes: set = set()
    try:
        initial_codes = set(email_providers.list_verification_codes(provider, inbox_url))
    except Exception:
        pass

    try:
        # 前置自检：enroll/UI 兜底可能已把会话折腾挂了，先确认浏览器还活着
        try:
            driver.current_url
            log_func("  🌐 浏览器自检通过")
        except Exception as exc:  # noqa: BLE001
            log_func(f"  ⚠️ 浏览器会话已失效，需要重建: {str(exc)[:80]}")
            return False

        log_func("  🌐 打开 chatgpt.com 准备登录 ...")
        driver.get("https://chatgpt.com")
        time.sleep(3)
        for button in driver.find_elements(By.XPATH, "//button[contains(., '登录') or contains(., 'Log in')]"):
            if button.is_displayed():
                log_func("  👉 点击登录入口")
                driver.execute_script("arguments[0].click();", button)
                time.sleep(2)
                break

        log_func("  📝 定位邮箱输入框 ...")
        email_input = WebDriverWait(driver, 25).until(
            EC.visibility_of_element_located((
                By.CSS_SELECTOR,
                'input[type="email"], input[name="email"], input[name="login_hint"], input[autocomplete="email"]',
            ))
        )
        # 用真实键盘逐字输入（JS 注入在新版弹窗上会被 React 重置导致"邮箱无效"）
        try:
            email_input.click()
        except Exception:
            driver.execute_script("arguments[0].click();", email_input)
        time.sleep(0.3)
        try:
            email_input.send_keys(Keys.CONTROL + "a")
            time.sleep(0.2)
        except Exception:
            pass
        type_slowly(email_input, email, delay=0.03)
        actual = (email_input.get_attribute("value") or "").strip()
        if actual != email:
            log_func(f"  ⚠️ 键盘输入未生效(实际={actual!r})，回退 JS 注入")
            _set_input_value(driver, email_input, email)
        log_func(f"  ✅ 已填入邮箱: {email}")
        time.sleep(0.6)
        submit = _find_email_submit_button(driver, email_input)
        if submit is not None:
            log_func("  👉 提交邮箱（稳健点击）")
            _click_and_confirm_submit(driver, submit, email_input)
        else:
            log_func("  ⚠️ 未找到提交按钮，回车提交")
            driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()

        step = _wait_for_post_email_step(driver, timeout=timeout)
        log_func(f"  🔀 登录第一步页面状态: {step}")

        # 有密码的账号**无条件优先**走「使用密码继续」——绕开邮箱验证码（官方常拒发/频控）。
        # 注意：无论页面被判成 login_with 还是 verification，「使用密码继续」切换都在页面上；
        # 且页面渲染有竞态（有时先只见验证码部分），所以这里不依赖分支判定，直接尝试点击。
        if password:
            log_func("  🔑 账号带密码，优先走密码登录（绕开邮箱验证码）...")
            from .browser import choose_password_login

            if choose_password_login(driver, password, log=log_func):
                ok = verify_logged_in(driver, timeout=45)
                log_func(f"  {'✅ 新鲜登录成功（密码方式）' if ok else '❌ 密码登录未确认'}")
                return ok
            log_func("  ↪️ 密码登录未成功，回退邮箱验证码方式...")
            step = _wait_for_post_email_step(driver, timeout=20)
            log_func(f"  🔀 回退后页面状态: {step}")

        if step == "login_with":

            # 该页是"输入我们刚刚发送的验证码"页；必要时点一次发送
            choose_email_code_login(driver, log=log_func)
            step = _wait_for_post_email_step(driver, timeout=30)
            log_func(f"  🔀 选择登录方式后: {step}")

        # 官方有时不会自动发码：主动点一次"重新发送电子邮件"
        def _click_resend() -> bool:
            for label in ("重新发送电子邮件", "重新发送", "Resend email", "Resend", "发送电子邮件", "发送验证码"):
                for xpath in (f"//button[contains(., '{label}')]", f"//a[contains(., '{label}')]"):
                    try:
                        for element in driver.find_elements(By.XPATH, xpath):
                            if element.is_displayed() and element.is_enabled():
                                driver.execute_script("arguments[0].click();", element)
                                log_func(f"  🔄 已点击「{label}」触发发送验证码")
                                return True
                    except Exception:
                        continue
            return False

        code = email_providers.wait_for_verification_email(
            provider, inbox_url, timeout=35, exclude_codes=initial_codes,
        )
        if not code:
            _click_resend()
            time.sleep(3)
            code = email_providers.wait_for_verification_email(
                provider, inbox_url, timeout=60, exclude_codes=initial_codes,
            )
        if not code:
            # 兜底：收件箱里未过期的既有验证码也可以直接用（官方频控时不再发新码）
            try:
                leftovers = [c for c in email_providers.list_verification_codes(provider, inbox_url) if c]
            except Exception:
                leftovers = []
            if leftovers:
                code = leftovers[0]
                log_func(f"  ↩️ 未收到新码，回退使用收件箱既有验证码: {code}")
        if not code:
            log_func("  ⚠️ 未收到登录验证码")
            return False
        log_func(f"  ✅ 登录验证码: {code}")
        enter_verification_code(driver, code)
        time.sleep(6)
        ok = verify_logged_in(driver, timeout=45)
        log_func(f"  {'✅ 新鲜登录成功' if ok else '❌ 新鲜登录未确认'}")
        return ok
    except Exception as exc:  # noqa: BLE001
        log_func(f"  ⚠️ 新鲜登录异常: {exc}")
        return False


def perfect_single_account(
    email: str,
    password: str | None = None,
    inbox_url: str | None = None,
    provider: str | None = None,
    proxy: dict | None = None,
    headless: bool = False,
    log_func: Callable[[str], None] = print,
) -> dict:
    """全自动完善单个账号（登录 → 真实凭据 → 真实 2FA → 真实权益检测 → 落盘）。"""
    log_func(f"⚡ 开始完善账号: {email} ...")

    accounts_file = _resolve_accounts_file()
    records = load_accounts_from_file(accounts_file) if os.path.exists(accounts_file) else []
    target_rec = next((r for r in records if r.get("email") == email), {})

    # 只使用真实存在的凭据；不再拼接"看起来像密码"的占位密码
    password = password or target_rec.get("password") or None
    if password in ("N/A", ""):
        password = None
    inbox_url = (
        inbox_url
        or target_rec.get("mailbox_credential")
        or target_rec.get("credential")
        or target_rec.get("inbox_url")
        or ""
    )
    provider = provider or target_rec.get("provider") or "mailtm"

    initial_codes = set()
    if inbox_url:
        try:
            initial_codes = set(email_providers.list_verification_codes(provider, inbox_url))
        except Exception:
            pass

    res = {
        "email": email,
        "password": password,
        "success": False,
        "two_factor_secret": "",
        "plan": "未检测",
        "is_plus": None,
        "trial_status": "待官方确认",
        "trial_eligible": None,
        "quota": "未知，请在官方页面确认",
        "expires_at": "未知，未取得订阅到期时间",
        "account_status": "⚪ 未在线验证",
        "error": None,
        "evidence_file": "",
        "state": None,
    }

    driver = None
    if proxy is None or not proxy.get("enabled"):
        proxy = _autodetect_proxy(log_func)

    try:
        log_func("  🌐 正在启动 Chrome 环境...")
        driver = create_driver(headless=headless, proxy=proxy)
        driver.get("https://chatgpt.com")
        time.sleep(3)

        # 首页可能已登录，也可能需要走登录流程
        already_logged_in = False
        try:
            already_logged_in = bool(driver.find_elements(By.CSS_SELECTOR, "textarea#prompt-textarea, button[data-testid='profile-button'], button[data-testid='user-menu-button']"))
        except Exception:
            already_logged_in = False

        # 优先用本地保存的真实 session cookie 恢复登录（免邮箱验证码，最可靠）
        if not already_logged_in:
            already_logged_in = _restore_saved_session(driver, email, log_func)

        if not already_logged_in:
            try:
                login_buttons = [
                    b for b in driver.find_elements(
                        By.XPATH, "//button[contains(., '登录') or contains(., 'Log in')]"
                    ) if b.is_displayed()
                ]
                if login_buttons:
                    driver.execute_script("arguments[0].click();", login_buttons[0])
                    time.sleep(1.5)
            except Exception:
                pass

            log_func("  📝 提交邮箱地址...")
            try:
                email_input = WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((
                        By.CSS_SELECTOR,
                        'input[name="login_hint"], input[name="email"], input[type="email"], #email-input, #email',
                    ))
                )
            except Exception:
                driver.get("https://chatgpt.com/auth/login")
                time.sleep(3)
                email_input = WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((
                        By.CSS_SELECTOR,
                        'input[name="login_hint"], input[name="email"], input[type="email"], #email-input, #email',
                    ))
                )

            # 用真实键盘逐字输入（JS 注入值在新版弹窗上会被 React 状态重置，
            # 实测出现"电子邮件地址无效/为必填项"；真实键入最可靠）
            try:
                email_input.click()
            except Exception:
                driver.execute_script("arguments[0].click();", email_input)
            time.sleep(0.3)
            try:
                email_input.send_keys(Keys.CONTROL + "a")
                time.sleep(0.2)
            except Exception:
                pass
            type_slowly(email_input, email, delay=0.03)
            actual = (email_input.get_attribute("value") or "").strip()
            if actual != email:
                log_func(f"  ⚠️ 键盘输入未生效(实际={actual!r})，回退 JS 注入")
                driver.execute_script(
                    """
                    const el = arguments[0], value = arguments[1];
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, value);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    """,
                    email_input, email,
                )
            else:
                log_func(f"  ✅ 已填入邮箱: {email}")
            time.sleep(0.6)

            # 用与注册流程一致的稳健点击（避免页面上多个 submit 按钮点空）
            from .browser import (
                _click_and_confirm_submit, _find_email_submit_button, _wait_for_post_email_step,
                choose_email_code_login,
            )

            submit_button = _find_email_submit_button(driver, email_input)
            if submit_button is not None:
                _click_and_confirm_submit(driver, submit_button, email_input)
            else:
                try:
                    driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
                except Exception:
                    email_input.send_keys(Keys.ENTER)
            time.sleep(2)
            # 等页面真正推进到密码页/验证码页，避免在错误的页面上瞎轮询邮箱
            step = _wait_for_post_email_step(driver, timeout=int(os.environ.get("GPT_POST_EMAIL_TIMEOUT", "45")))
            log_func(f"  🔀 邮箱提交后页面状态: {step}")

            # 已有账号且无密码时会进入"选择登录方式"页：选邮箱验证码继续
            if step == "login_with":
                if choose_email_code_login(driver, log=log_func):
                    step = _wait_for_post_email_step(
                        driver, timeout=int(os.environ.get("GPT_POST_EMAIL_TIMEOUT", "45"))
                    )
                    log_func(f"  🔀 选择邮箱验证码后的页面状态: {step}")

            password_fields = [
                f for f in driver.find_elements(By.CSS_SELECTOR, 'input[type="password"], input[name="password"]')
                if f.is_displayed()
            ]

            if password_fields and password:
                log_func("  🔑 检测到密码登录页，输入账号密码...")
                type_slowly(password_fields[0], password)
                time.sleep(0.5)
                try:
                    driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
                except Exception:
                    driver.switch_to.active_element.send_keys(Keys.ENTER)
                time.sleep(4)

                totp_fields = [
                    t for t in driver.find_elements(
                        By.CSS_SELECTOR, 'input[autocomplete="one-time-code"], input[name="code"]'
                    ) if t.is_displayed()
                ]
                existing_secret = (target_rec.get("two_factor_secret") or "").strip()
                if totp_fields and existing_secret and existing_secret not in ("未开启", "N/A"):
                    from .two_factor_service import TwoFactorService

                    log_func("  🔐 账号已开启 2FA，输入动态验证码完成登录...")
                    totp_value = TwoFactorService.get_current_totp(existing_secret)
                    totp_fields[0].send_keys(totp_value)
                    time.sleep(0.5)
                    try:
                        driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
                    except Exception:
                        driver.switch_to.active_element.send_keys(Keys.ENTER)
                    time.sleep(5)
                elif totp_fields:
                    log_func("  ⚠️ 登录要求 2FA 验证码，但本地没有该账号的真实密钥，需先重置 2FA")
            else:
                log_func("  📩 账号走邮箱验证码分支，正在从收件箱拉取最新验证码...")
                if not inbox_url:
                    raise RuntimeError(f"账号为验证码分支，但本地缺少收件箱凭据: {email}")
                otp_code = None
                wait_timeout = 60
                start = time.time()
                resent = False
                while time.time() - start < wait_timeout:
                    time.sleep(2)
                    try:
                        codes = [c for c in email_providers.list_verification_codes(provider, inbox_url)
                                 if c and c not in initial_codes]
                    except Exception:
                        codes = []
                    if codes:
                        otp_code = codes[0]
                        break
                    if time.time() - start > 20 and not resent:
                        resent = True
                        try:
                            for button in driver.find_elements(
                                By.XPATH,
                                "//button[contains(., '重新发送') or contains(., 'Resend')] | "
                                "//a[contains(., '重新发送') or contains(., 'Resend')]",
                            ):
                                if button.is_displayed():
                                    log_func("  🔄 超过 20 秒未收到验证码，自动点击【重新发送电子邮件】...")
                                    button.click()
                                    time.sleep(1)
                                    break
                        except Exception:
                            pass
                if not otp_code:
                    raise RuntimeError("等待最新邮箱验证码超时（可能受官方临时频控）")
                log_func(f"  ✅ 收到验证码: {otp_code}，正在填入...")
                enter_verification_code(driver, otp_code)
                time.sleep(6)

        log_func("  🎉 已进入 ChatGPT 会话")

        # 1. 真实 Web 凭据
        session_info = _capture_session(driver, email, log_func)

        # 2. 真实 2FA（官方确认后才写入）
        two_factor_secret = ""
        existing_secret = (target_rec.get("two_factor_secret") or "").strip()
        if existing_secret and existing_secret not in ("未开启", "N/A"):
            two_factor_secret = existing_secret
            log_func("  ℹ️ 本地已有真实 2FA 密钥，跳过重复绑定")
        else:
            log_func("  🔐 正在真实开启 2FA (TOTP)...")
            mfa = enable_account_2fa(driver, account_password=password or "", log_func=log_func)
            if mfa.get("success"):
                two_factor_secret = mfa.get("secret", "")
                log_func(f"  ✅ 官方确认 2FA 生效，密钥: {two_factor_secret}")
            else:
                log_func(f"  ⚠️ 2FA 未完成: {mfa.get('error')}（不写入伪造密钥；证据已记录）")
            res["two_factor_evidence"] = mfa.get("evidence", [])

        # 3. 真实权益状态
        log_func("  🔍 正在采集真实订阅状态与 1 个月 Plus 试用资格...")
        state = _collect_state(driver, email, session_info, proxy, log_func)
        evidence_file = save_state_evidence(email, state)
        res.update({
            "plan": state["plan"],
            "is_plus": state.get("is_plus"),
            "trial_status": state["trial_status"],
            "trial_eligible": state.get("trial_eligible"),
            "quota": state["quota"],
            "expires_at": state["expires_at"],
            "account_status": state["account_status"],
            "two_factor_secret": two_factor_secret,
            "evidence_file": evidence_file,
            "state": state,
        })
        log_func(
            f"  📊 真实结果: 计划={state['plan']} | Plus={state.get('is_plus')} | "
            f"试用={state['trial_status']} | 到期={state['expires_at']}"
        )

        # 4. 免密账号：优先在官方设置页「密码 → 添加」设置真实密码，失败再走忘记密码邮件流程
        new_password = password
        if not new_password:
            new_password = generate_random_password()
            if set_password_via_settings(driver, new_password, log_func,
                                         provider=provider, inbox_url=inbox_url):
                res["password_auto_set"] = True
            else:
                log_func("  ↪️ 设置页添加密码未成功，改用官方「忘记密码」邮件流程...")
                new_password = set_initial_password(
                    driver, email, provider, inbox_url, initial_codes, log_func
                )
                if new_password:
                    res["password_auto_set"] = True
            if not new_password:
                log_func("  ℹ️ 未能自动设置密码，密码列保持 N/A（不编造）")

        update_account_check_result_in_file(
            accounts_file,
            email,
            state["plan"],
            state["trial_status"],
            quota=state["quota"],
            expires_at=state["expires_at"],
            account_status=state["account_status"],
            two_factor_secret=two_factor_secret or None,
            evidence=state_evidence_summary(state, evidence_file),
        )
        if new_password:
            save_to_txt(email, new_password, target_rec.get("status") or "已注册", provider=provider)

        res["password"] = new_password
        res["success"] = bool(state.get("verified")) or bool(two_factor_secret)
        if not res["success"]:
            res["error"] = "未取得官方确认的数据（凭证或接口被拦截）"
        log_func(f"  ✅ 账号 [{email}] 完善流程结束")
        return res

    except Exception as exc:  # noqa: BLE001
        log_func(f"  ❌ 完善账号异常: {exc}")
        res["error"] = str(exc)
        return res
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def perfect_accounts_batch(
    emails: list[str],
    *,
    proxy: dict | None = None,
    headless: bool = True,
    workers: int = 2,
    log_func: Callable[[str], None] = print,
    stop_check: Callable[[], bool] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict:
    """并发完善多个账号（注册流水线的"补全"阶段）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(emails)
    summary = {"total": total, "success": 0, "fail": 0, "results": []}
    if not total:
        return summary

    workers = max(1, min(workers, total))
    done = 0

    def _one(item_email: str) -> dict:
        if stop_check and stop_check():
            return {"email": item_email, "success": False, "error": "stopped"}
        return perfect_single_account(
            email=item_email, proxy=proxy, headless=headless,
            log_func=lambda message: log_func(f"[{item_email}] {message}"),
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_one, item): item for item in emails}
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = {"email": item, "success": False, "error": str(exc)}
            summary["results"].append(result)
            summary["success" if result.get("success") else "fail"] += 1
            done += 1
            if progress:
                progress(done, total, item)
    return summary
