"""
浏览器自动化模块 - ChatGPT 注册流程
"""

import io
import json
import os
import sys
import re
import socket
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
from datetime import datetime
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

from .config import (
    MAX_WAIT_TIME,
    SHORT_WAIT_TIME,
    ERROR_PAGE_MAX_RETRIES,
    BUTTON_CLICK_MAX_RETRIES,
    PROJECT_ROOT,
)
from .utils import generate_user_info


def dump_page_diagnostics(driver, tag: str = "signup") -> dict:
    """把当前页面真实现场落盘（URL/标题/正文/截图/源码），失败时用于复盘。

    输出目录: ``gpt-auto-register/data/diagnostics/``
    """
    info = {"tag": tag, "url": "", "title": "", "text": "", "screenshot": "", "html": ""}
    try:
        info["url"] = str(driver.current_url or "")
    except Exception:
        pass
    try:
        info["title"] = str(driver.title or "")
    except Exception:
        pass
    try:
        info["text"] = (driver.find_element(By.TAG_NAME, "body").text or "")[:2000]
    except Exception:
        pass

    diagnostics_dir = PROJECT_ROOT / "data" / "diagnostics"
    try:
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            screenshot = driver.get_screenshot_as_png()
            shot_path = diagnostics_dir / f"{tag}_{stamp}.png"
            shot_path.write_bytes(screenshot)
            info["screenshot"] = str(shot_path)
        except Exception:
            pass
        try:
            html_path = diagnostics_dir / f"{tag}_{stamp}.html"
            html_path.write_text(driver.page_source or "", encoding="utf-8", errors="replace")
            info["html"] = str(html_path)
        except Exception:
            pass
        try:
            (diagnostics_dir / f"{tag}_{stamp}.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
    except Exception:
        pass

    print("🧪 页面现场诊断:")
    print(f"   URL   : {info['url']}")
    print(f"   标题  : {info['title']}")
    if info["text"]:
        preview = " / ".join(line.strip() for line in info["text"].splitlines() if line.strip())[:300]
        print(f"   正文  : {preview}")
    if info["screenshot"]:
        print(f"   截图  : {info['screenshot']}")
    if info["html"]:
        print(f"   源码  : {info['html']}")
    return info


def _is_cloudflare_challenge(driver) -> bool:
    try:
        title = (driver.title or "").lower()
    except Exception:
        title = ""
    if "just a moment" in title or "请稍候" in title:
        return True
    text = _page_text(driver)
    return "ray id" in text or "cf-challenge" in text or "challenge-platform" in text


def _visible_error_text(driver) -> str:
    """抓取页面上真实的报错文案（OpenAI 拒绝注册时一定会有提示）。"""
    for selector in (
        '[role="alert"]',
        '[class*="error" i]',
        '[data-testid*="error" i]',
        'div[class*="text-red" i]',
        'span[class*="error" i]',
    ):
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed():
                    text = (element.text or "").strip()
                    if text:
                        return text[:300]
        except Exception:
            continue
    return ""


def _page_text(driver) -> str:
    try:
        return (driver.page_source or "").lower()
    except Exception:
        return ""


def _visible_body_text(driver) -> str:
    """只取可见正文文本（避免 JS bundle 里的字符串造成误判）。"""
    try:
        return (driver.find_element(By.TAG_NAME, "body").text or "").lower()
    except Exception:
        return ""


def _is_email_verification_page(driver) -> bool:
    try:
        current_url = (driver.current_url or "").lower()
    except Exception:
        current_url = ""

    if any(token in current_url for token in ["email-verification", "verification", "/code", "enter-code"]):
        return True

    page_text = _visible_body_text(driver)
    text_markers = [
        "检查您的收件箱",
        "检查你的收件箱",
        "验证邮箱",
        "验证码",
        "verify your email",
        "check your inbox",
        "输入我们刚刚向",
        "enter code",
        "verification code",
    ]
    if any(marker in page_text for marker in text_markers):
        return True

    verification_selectors = [
        'input[name="code"]',
        'input[name*="code"]',
        'input[autocomplete="one-time-code"]',
        'input[placeholder*="代码"]',
        'input[placeholder*="验证码"]',
        'input[placeholder*="code" i]',
        'input[aria-label*="代码"]',
        'input[aria-label*="验证码"]',
        'input[aria-label*="code" i]',
    ]
    for selector in verification_selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if any(el.is_displayed() for el in elements):
                return True
        except Exception:
            continue

    try:
        otp_boxes = driver.find_elements(
            By.CSS_SELECTOR,
            'input[inputmode="numeric"], input[autocomplete="one-time-code"], input[maxlength="1"]',
        )
        visible_count = sum(1 for el in otp_boxes if el.is_displayed())
        if visible_count >= 4:
            return True
    except Exception:
        pass

    return False


def check_email_already_registered(driver) -> bool:
    """检测页面是否提示该邮箱已在官方注册过账号"""
    try:
        page_text = _page_text(driver)
        keywords = [
            "already have an account",
            "already exists",
            "user already exists",
            "already registered",
            "an account already exists",
            "already associated with an account",
            "用户已存在",
            "该邮箱已注册",
            "此邮箱已注册",
            "此邮箱已被注册",
            "邮箱已被使用",
            "已经注册过",
            "该邮箱已存在",
            "there is already an account",
        ]
        if any(k in page_text for k in keywords):
            return True
        try:
            cur_pwd = driver.find_elements(By.CSS_SELECTOR, 'input[autocomplete="current-password"]')
            new_pwd = driver.find_elements(By.CSS_SELECTOR, 'input[autocomplete="new-password"]')
            if any(el.is_displayed() for el in cur_pwd) and not any(el.is_displayed() for el in new_pwd):
                return True
        except Exception:
            pass
        return False
    except Exception:
        return False


def _find_email_submit_button(driver, email_input, force: bool = False):
    """定位"继续"按钮。

    新版 ChatGPT 是首页页内弹窗注册，页面上可能存在多个 ``button[type=submit]``
    （历史上取第一个会点空按钮，导致弹窗毫无反应）。这里按可靠性依次尝试：
      1. 邮箱输入框所在 form / dialog / 弹窗容器内的可见提交按钮
      2. 全局可见且文案为 继续/Continue/下一步 的按钮
      3. 全局最后一个可见提交按钮（弹窗通常挂在 DOM 末尾）
    """
    candidates: list = []

    for container_xpath in (
        "./ancestor::form[1]",
        "./ancestor::*[@role='dialog'][1]",
        "./ancestor::*[contains(@class,'modal')][1]",
        "./ancestor::*[contains(@class,'dialog')][1]",
    ):
        try:
            container = email_input.find_element(By.XPATH, container_xpath)
        except Exception:
            continue
        try:
            found = [
                element for element in container.find_elements(By.CSS_SELECTOR, 'button[type="submit"], button')
                if element.is_displayed() and element.is_enabled()
            ]
        except Exception:
            continue
        if found:
            candidates.extend(reversed(found))

    for label in ("继续", "Continue", "下一步", "Next", "注册", "Sign up"):
        try:
            for element in driver.find_elements(
                By.XPATH, f"//button[contains(normalize-space(.), '{label}')]"
            ):
                if element.is_displayed() and element.is_enabled():
                    candidates.append(element)
        except Exception:
            continue

    try:
        submits = [
            element for element in driver.find_elements(By.CSS_SELECTOR, 'button[type="submit"]')
            if element.is_displayed() and element.is_enabled()
        ]
        candidates.extend(reversed(submits))
    except Exception:
        pass

    seen = set()
    for element in candidates:
        try:
            fingerprint = element.id
        except Exception:
            fingerprint = id(element)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        try:
            if element.is_displayed() and element.is_enabled():
                return element
        except Exception:
            continue
    return None


def _click_and_confirm_submit(driver, button, email_input, wait_seconds: float = 6.0) -> bool:
    """点击提交按钮，并在点击无效时用备用方式重试（原生点击 / JS 点击 / 回车提交）。"""
    def _value() -> str:
        try:
            return (email_input.get_attribute("value") or "").strip()
        except Exception:
            return ""

    def _still_on_email_step() -> bool:
        try:
            if not email_input.is_displayed():
                return False
        except Exception:
            return False
        return not _is_email_verification_page(driver)

    attempts = (
        ("原生点击", lambda: button.click()),
        ("JS 点击", lambda: driver.execute_script("arguments[0].click();", button)),
        ("回车提交", lambda: email_input.send_keys(Keys.ENTER)),
    )
    for index, (name, action) in enumerate(attempts):
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            print(f"   ⚠️ {name}失败: {str(exc)[:120]}")
            continue
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if not _still_on_email_step():
                print(f"   ✅ 邮箱已提交（{name} 生效）")
                return True
            time.sleep(0.4)
        if index < len(attempts) - 1:
            print(f"   ↪️ {name} 后页面无变化，改用下一种提交方式...")
        _value()  # 保持输入框引用有效（部分 SPA 会重建节点）
    print("   ⚠️ 三种提交方式后仍未检测到页面推进")
    return False


def open_security_settings(driver, timeout: int = 25) -> bool:
    """稳妥地打开「设置 → 安全防护」页。

    实测仅用 ``#settings/Security`` 的 hash 导航有时不渲染（拿到空正文），
    所以这里做成：hash 导航 → 等待正文出现 → 不行就点用户菜单走一遍 UI。
    """
    def _settings_visible() -> bool:
        try:
            text = driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            return False
        markers = ("安全防护", "账户安全", "Security", "多因素", "MFA", "密码", "设置")
        return any(marker in text for marker in markers)

    for _ in range(20):
        try:
            driver.get(f"https://chatgpt.com/#settings/Security")
        except Exception:
            pass
        for _ in range(timeout):
            if _settings_visible():
                print("  ✅ 设置页已渲染")
                time.sleep(1.5)
                return True
            time.sleep(1)

        # 备用：点用户菜单 → 设置 → 安全防护
        print("  ↻ hash 导航未渲染，改用菜单点击打开设置 ...")
        try:
            menu = [
                element for element in driver.find_elements(
                    By.CSS_SELECTOR,
                    'button[data-testid*="user-menu"], button[data-testid*="profile"], '
                    'button[aria-label*="Profile"], button[aria-label*="profile"], img[alt*="User"]',
                ) if element.is_displayed()
            ]
            if menu:
                driver.execute_script("arguments[0].click();", menu[0])
                time.sleep(2)
                for label in ("设置", "Settings"):
                    items = [
                        element for element in driver.find_elements(
                            By.XPATH, f"//*[self::button or self::div or self::a][contains(normalize-space(.), '{label}')]"
                        ) if element.is_displayed()
                    ]
                    if items:
                        driver.execute_script("arguments[0].click();", items[-1])
                        time.sleep(2)
                        break
                for label in ("安全防护", "安全", "Security"):
                    tabs = [
                        element for element in driver.find_elements(
                            By.XPATH, f"//*[self::button or self::div or self::a][contains(normalize-space(.), '{label}')]"
                        ) if element.is_displayed()
                    ]
                    if tabs:
                        driver.execute_script("arguments[0].click();", tabs[0])
                        time.sleep(2)
                        break
                if _settings_visible():
                    print("  ✅ 设置页已渲染（菜单路径）")
                    return True
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 菜单路径失败: {str(exc)[:80]}")

    dump_page_diagnostics(driver, "settings_open_failed")
    return False


def _is_login_method_chooser(driver) -> bool:
    """是否是官方"选择登录方式"页（已有账号且无密码时会进入）。

    实测 URL: ``https://chatgpt.com/auth/login_with?callback_path=%2F&screen_hint=login_or_signup&login_hint=...``
    """
    try:
        current_url = (driver.current_url or "").lower()
    except Exception:
        current_url = ""
    if "login_with" in current_url:
        return True
    text = _visible_body_text(driver)
    markers = ("使用邮箱", "使用电子邮件", "continue with email", "email code", "一次性验证码", "登录方式")
    return any(marker in text for marker in markers)


def choose_email_code_login(driver, log=print) -> bool:
    """在"选择登录方式"页选择「用邮箱验证码继续」。

    实测该页有时会白屏（React 未 hydrate）——先重载并等正文渲染出来，再按可见文本点击。
    选项可能是 button / a / role=button / role=radio / label / 可点击行，所以不做元素类型限制。
    """
    labels = (
        "使用邮箱", "使用电子邮件", "邮箱验证码", "发送验证码", "发送电子邮件", "通过电子邮件",
        "邮箱继续", "Email me", "Continue with email", "Use email", "Email code",
        "one-time code", "一次性验证码", "验证码", "Email", "邮箱",
    )

    def _visible_text() -> str:
        try:
            return (driver.find_element(By.TAG_NAME, "body").text or "").strip()
        except Exception:
            return ""

    def _click_first_match() -> bool:
        for label in labels:
            for xpath in (
                f"//button[contains(normalize-space(.), '{label}')]",
                f"//a[contains(normalize-space(.), '{label}')]",
                f"//*[@role='button'][contains(normalize-space(.), '{label}')]",
                f"//*[@role='radio'][contains(normalize-space(.), '{label}')]",
                f"//label[contains(normalize-space(.), '{label}')]",
            ):
                try:
                    candidates = driver.find_elements(By.XPATH, xpath)
                except Exception:
                    continue
                for element in candidates:
                    try:
                        if element.is_displayed() and element.is_enabled():
                            driver.execute_script("arguments[0].click();", element)
                            log(f"  ✅ 已选择登录方式: {label}")
                            time.sleep(3)
                            return True
                    except Exception:
                        continue
        return False

    # 白屏时先重载，最多尝试 2 轮
    for attempt in range(1, 3):
        text = _visible_text()
        if not text:
            log(f"  ↻ 登录方式页正文为空（第 {attempt} 次），重载页面等待渲染...")
            try:
                current_url = driver.current_url
                driver.get(current_url)
            except Exception:
                pass
            for _ in range(20):
                time.sleep(1)
                text = _visible_text()
                if text:
                    break
        if text:
            preview = " / ".join(line.strip() for line in text.splitlines() if line.strip())[:200]
            log(f"  📄 登录方式页正文: {preview}")
            if _click_first_match():
                return True

    log("  ⚠️ 未在登录方式选择页找到邮箱验证码入口（现场已保存诊断）")
    dump_page_diagnostics(driver, "login_method_chooser")
    return False


def choose_password_login(driver, password: str, log=print) -> bool:
    """在"选择登录方式"页选择「使用密码继续」，并填入密码提交。

    协议版注册的账号自带密码：走密码登录可**绕开邮箱验证码**（免密账号才被迫用邮箱码），
    这是"注册即带密码 + 快速重登"的关键路径。

    实测该页有渲染竞态（React 时而白屏），所以进入后先等正文出现，最多重载 2 次。
    """
    def _page_ready() -> bool:
        try:
            return bool((driver.find_element(By.TAG_NAME, "body").text or "").strip())
        except Exception:
            return False

    for attempt in range(1, 4):
        if _page_ready():
            break
        log(f"  ↻ 登录页正文为空（第 {attempt} 次），等待/重载渲染 ...")
        for _ in range(15):
            time.sleep(1)
            if _page_ready():
                break
        if not _page_ready():
            try:
                driver.get(driver.current_url)
                time.sleep(3)
            except Exception:
                pass

    clicked = False
    for label in ("使用密码", "密码继续", "Continue with password", "Use password", "密码登录"):
        for xpath in (
            f"//button[contains(normalize-space(.), '{label}')]",
            f"//a[contains(normalize-space(.), '{label}')]",
            f"//*[@role='button'][contains(normalize-space(.), '{label}')]",
        ):
            try:
                for element in driver.find_elements(By.XPATH, xpath):
                    if element.is_displayed() and element.is_enabled():
                        driver.execute_script("arguments[0].click();", element)
                        log(f"  ✅ 已选择「{label}」")
                        clicked = True
                        time.sleep(3)
                        break
            except Exception:
                continue
            if clicked:
                break
        if clicked:
            break
    if not clicked:
        # 兜底：页面上的选项可能是 div/span 包裹的可点击行。
        # 找文本含"使用密码/密码继续"的**最小可见元素**（避免点到整页容器）。
        try:
            candidates = driver.find_elements(
                By.XPATH,
                "//*[contains(normalize-space(.), '使用密码') or contains(normalize-space(.), '密码继续') "
                "or contains(normalize-space(.), 'Continue with password')]",
            )
            best = None
            best_len = 10**9
            for element in candidates:
                try:
                    if not element.is_displayed():
                        continue
                    text = (element.text or "").strip()
                    if text and len(text) < best_len:
                        best = element
                        best_len = len(text)
                except Exception:
                    continue
            if best is not None:
                driver.execute_script("arguments[0].click();", best)
                log(f"  ✅ 已点击密码入口（最小元素: {(best.text or '').strip()[:20]!r}）")
                clicked = True
                time.sleep(3)
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠️ 密码入口兜底查找失败: {str(exc)[:80]}")

    if not clicked:
        # 页面可能已直接展示密码输入框
        try:
            if not driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]'):
                log("  ⚠️ 未找到密码入口（页面上也无密码框），输出现场诊断")
                dump_page_diagnostics(driver, "password_entry_missing")
                return False
        except Exception:
            return False

    try:
        fields = [
            element for element in driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]')
            if element.is_displayed()
        ]
        if not fields:
            log("  ⚠️ 未找到密码输入框")
            return False
        try:
            fields[0].click()
        except Exception:
            driver.execute_script("arguments[0].click();", fields[0])
        time.sleep(0.3)
        type_slowly(fields[0], password, delay=0.04)
        log("  ✅ 已输入密码")
        time.sleep(0.6)
        for element in driver.find_elements(
            By.XPATH, "//button[@type='submit' or contains(normalize-space(.), '继续') "
            "or contains(normalize-space(.), 'Continue') or contains(normalize-space(.), '登录')]"
        ):
            if element.is_displayed() and element.is_enabled():
                driver.execute_script("arguments[0].click();", element)
                log("  👉 已提交密码")
                break
        time.sleep(5)
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠️ 密码登录异常: {str(exc)[:100]}")
        return False


def _switch_to_password_signup(driver, log=print) -> bool:
    """在邮箱验证码页点击「使用密码继续」，切到密码创建页（让账号带上密码）。

    移植自公开协议实现的实测选择器：验证码页上存在 a[href*='/create-account/password']
    链接（"使用密码继续"），点击后官方切换到 /create-account/password 设密码；
    找不到链接时兜底直接导航到该 URL。
    """
    selectors = (
        "a[href*='/create-account/password']",
        "[data-login-web-auth-control='true'][href*='/create-account/password']",
        "[role='link'][href*='/create-account/password']",
    )
    for selector in selectors:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed() and element.is_enabled():
                    driver.execute_script("arguments[0].click();", element)
                    log("  🔑 已点击「使用密码继续」链接，切换到密码创建页")
                    time.sleep(4)
                    return True
        except Exception:
            continue

    # 属性兜底：找任何 href/属性里含 create-account/password 的可点元素
    try:
        result = driver.execute_script(
            """
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
            const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
            const btn = [...document.querySelectorAll('a,button,[role="link"],[role="button"]')]
              .filter(el => visible(el) && enabled(el))
              .find(el => {
                const href = String(el.getAttribute('href') || '').toLowerCase();
                const attrs = [href, el.id, el.getAttribute('aria-label'), el.getAttribute('title'),
                  el.getAttribute('data-login-web-auth-control'), el.className].join(' ').toLowerCase();
                return href.includes('/create-account/password') || attrs.includes('/create-account/password');
              });
            if (!btn) return null;
            const href = btn.getAttribute('href');
            if (href) { btn.scrollIntoView({block:'center'}); btn.click(); return href; }
            return null;
            """
        )
        if result:
            log(f"  🔑 已通过属性匹配点击密码入口: {result}")
            time.sleep(4)
            return True
    except Exception:
        pass

    # 最终兜底：直接导航到密码创建页（官方允许已验证邮箱的会话直接访问）
    try:
        log("  ↪️ 页面上未找到密码入口，直接导航到 /create-account/password ...")
        driver.get("https://auth.openai.com/create-account/password")
        time.sleep(4)
        for _ in range(10):
            try:
                if driver.find_elements(By.CSS_SELECTOR, 'input[type="password"], input[name="password"]'):
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠️ 密码页导航失败: {str(exc)[:100]}")
        return False


def _wait_for_post_email_step(driver, timeout: int = 45) -> str:
    end_time = time.time() + timeout
    print(f"🔀 等待密码或验证码页面...（最长 {timeout}s）")
    last_heartbeat = time.time()
    while time.time() < end_time:
        if check_email_already_registered(driver):
            print("⏩ 检测到页面提示邮箱已在官方注册过 GPT 账号")
            return "already_registered"

        try:
            password_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[autocomplete="new-password"], input[type="password"]')
            if any(el.is_displayed() for el in password_inputs):
                if check_email_already_registered(driver):
                    return "already_registered"
                print("✅ 检测到密码页，继续输入密码")
                return "password"
        except Exception:
            pass

        # login_with（含"使用密码继续"选项）必须在验证码判定**之前**检查：
        # 该页正文里有"验证码"字样，会被误判成验证码页，导致密码分支永远走不到
        if _is_login_method_chooser(driver):
            print("🔀 检测到『选择登录方式』页（该邮箱已有账号，可密码/邮箱码登录）")
            return "login_with"

        if _is_email_verification_page(driver):
            print("✅ 检测到验证码页，跳过密码设置")
            return "verification"

        if _is_cloudflare_challenge(driver):
            print("🛡️ 检测到 Cloudflare 质询页，等待其自动通过...")

        error_text = _visible_error_text(driver)
        if error_text:
            print(f"⚠️ 页面上出现官方提示: {error_text}")

        now = time.time()
        if now - last_heartbeat >= 5:
            last_heartbeat = now
            try:
                current_url = driver.current_url
            except Exception:
                current_url = "N/A"
            print(f"   ⏳ 等待页面跳转... 当前: {current_url}")

        time.sleep(0.5)

    if check_email_already_registered(driver):
        print("⏩ 检测到页面提示邮箱已在官方注册过 GPT 账号")
        return "already_registered"

    print(f"❌ 邮箱提交后 {timeout}s 内未识别到密码页或验证码页，下面输出真实现场：")
    dump_page_diagnostics(driver, "post_email_step")
    return "unknown"


class SafeChrome(uc.Chrome):
    """
    自定义 Chrome 类，修复 Windows 下退出时的 WinError 6
    """

    def __del__(self):
        try:
            self.quit()
        except OSError:
            pass
        except Exception:
            pass

    def quit(self):
        try:
            super().quit()
        except OSError:
            pass
        except Exception:
            pass


# ──────────────────────────────────────────────────────────
# SOCKS5 带凭证本地中继
# Chrome 不支持在 --proxy-server 里内嵌 SOCKS5 凭证，也不支持扩展拦截
# SOCKS5 握手，所以用本地无认证中继让 Chrome 连过来，再由中继向上游注入凭证。
# ──────────────────────────────────────────────────────────

def _relay_pipe(src: socket.socket, dst: socket.socket):
    """单向管道：把 src 收到的数据原样转发给 dst。"""
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try:
                s.close()
            except Exception:
                pass


def _handle_socks5_client(client_sock: socket.socket,
                          upstream_host: str, upstream_port: int,
                          username: str, password: str):
    """
    处理 Chrome 发来的 SOCKS5 连接（无认证），
    并通过 PySocks 以凭证连接到上游 SOCKS5 代理。
    """
    try:
        import socks as _pysocks

        # 1. 读取客户端握手（SOCKS5 版本 + 可接受的认证方法列表）
        header = client_sock.recv(2)
        if len(header) < 2 or header[0] != 5:
            return
        n_methods = header[1]
        client_sock.recv(n_methods)          # 忽略客户端声明的方法列表

        # 2. 回复"无需认证"
        client_sock.sendall(b'\x05\x00')

        # 3. 读取 CONNECT 请求
        req = client_sock.recv(4)
        if len(req) < 4 or req[0] != 5 or req[1] != 1:
            client_sock.sendall(b'\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00')
            return

        atyp = req[3]
        if atyp == 0x01:        # IPv4
            addr = socket.inet_ntoa(client_sock.recv(4))
        elif atyp == 0x03:      # 域名
            length = client_sock.recv(1)[0]
            addr = client_sock.recv(length).decode('utf-8', errors='replace')
        elif atyp == 0x04:      # IPv6
            addr = socket.inet_ntop(socket.AF_INET6, client_sock.recv(16))
        else:
            client_sock.sendall(b'\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00')
            return

        port = struct.unpack('!H', client_sock.recv(2))[0]

        # 4. 通过 PySocks 连接上游（携带凭证）
        upstream_sock = _pysocks.socksocket()
        upstream_sock.set_proxy(
            _pysocks.SOCKS5, upstream_host, upstream_port,
            username=username, password=password
        )
        upstream_sock.connect((addr, port))

        # 5. 告诉 Chrome 连接成功
        client_sock.sendall(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')

        # 6. 双向转发数据
        t = threading.Thread(
            target=_relay_pipe, args=(upstream_sock, client_sock), daemon=True
        )
        t.start()
        _relay_pipe(client_sock, upstream_sock)
        t.join(timeout=5)

    except Exception as e:
        print(f"  ⚠️ SOCKS5 中继连接错误: {e}")
        try:
            client_sock.sendall(b'\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00')
        except Exception:
            pass
    finally:
        try:
            client_sock.close()
        except Exception:
            pass


class _Socks5AuthRelay:
    """
    全局单例：本地 SOCKS5 无认证中继 → 上游 SOCKS5 带凭证。
    配置不变时复用同一端口，无需重启。
    """

    def __init__(self):
        self._server: socket.socket | None = None
        self._port: int | None = None
        self._config: tuple | None = None
        self._lock = threading.Lock()

    def start(self, host: str, port: int, username: str, password: str) -> int:
        """启动中继（如已用相同配置启动则直接返回端口）。"""
        new_config = (host, port, username, password)
        with self._lock:
            if self._server and self._config == new_config:
                return self._port        # type: ignore[return-value]
            self._stop_locked()

            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('127.0.0.1', 0))
            local_port = srv.getsockname()[1]
            srv.listen(64)
            self._server = srv
            self._port = local_port
            self._config = new_config

            def _accept_loop():
                while True:
                    try:
                        client, _ = srv.accept()
                    except Exception:
                        break
                    threading.Thread(
                        target=_handle_socks5_client,
                        args=(client, host, port, username, password),
                        daemon=True,
                    ).start()

            threading.Thread(target=_accept_loop, daemon=True).start()
            print(f"  🔄 SOCKS5 本地中继: 127.0.0.1:{local_port} → {host}:{port}")
            return local_port

    def _stop_locked(self):
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
            self._port = None
            self._config = None


_socks5_relay = _Socks5AuthRelay()


def _build_proxy_extension(
    scheme: str, host: str, port: int, username: str, password: str
) -> str:
    """
    为有凭证的代理创建 Chrome 扩展 zip 文件（写入临时目录）。
    返回 zip 文件路径。
    无凭证代理不需要扩展，直接用 --proxy-server 参数即可。
    """
    manifest = """{
  "version": "1.0.0",
  "manifest_version": 2,
  "name": "Proxy Auth Extension",
  "permissions": [
    "proxy", "tabs", "unlimitedStorage", "storage",
    "<all_urls>", "webRequest", "webRequestBlocking"
  ],
  "background": { "scripts": ["background.js"] },
  "minimum_chrome_version": "22.0.0"
}"""
    background = f"""
var config = {{
  mode: "fixed_servers",
  rules: {{
    singleProxy: {{ scheme: "{scheme}", host: "{host}", port: {port} }},
    bypassList: ["localhost", "127.0.0.1"]
  }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});
chrome.webRequest.onAuthRequired.addListener(
  function(details) {{
    return {{ authCredentials: {{ username: "{username}", password: "{password}" }} }};
  }},
  {{urls: ["<all_urls>"]}},
  ["blocking"]
);
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("manifest.json", manifest)
        zf.writestr("background.js", background)
    buf.seek(0)
    ext_path = os.path.join(tempfile.gettempdir(), f"proxy_ext_{host}_{port}.zip")
    with open(ext_path, "wb") as f:
        f.write(buf.read())
    return ext_path


def _detect_chrome_major_version() -> int | None:
    """检测本机 Chrome 主版本，避免与 UC driver 主版本不匹配。"""
    # 1. 尝试 Windows 注册表快速查询
    if sys.platform == "win32":
        try:
            reg_cmds = [
                ['reg', 'query', r'HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon', '/v', 'version'],
                ['reg', 'query', r'HKEY_LOCAL_MACHINE\Software\Google\Chrome\BLBeacon', '/v', 'version'],
                ['reg', 'query', r'HKEY_LOCAL_MACHINE\Software\WOW6432Node\Google\Chrome\BLBeacon', '/v', 'version'],
            ]
            for cmd in reg_cmds:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False)
                out = (res.stdout or "").strip()
                m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
                if m:
                    major = int(m.group(1))
                    print(f"  🧩 [Windows Registry] 检测到本机 Chrome 主版本: {major}")
                    return major
        except Exception:
            pass

    # 2. 检查常见可执行文件路径
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "google-chrome",
        "chromium",
    ]
    for binary in candidates:
        if not os.path.exists(binary) and not sys.platform.startswith("linux"):
            continue
        try:
            if sys.platform == "win32":
                ps_cmd = f"(Get-Item '{binary}').VersionInfo.ProductVersion"
                result = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=5, check=False)
                output = (result.stdout or "").strip()
            else:
                result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5, check=False)
                output = (result.stdout or result.stderr or "").strip()

            match = re.search(r"(\d+)\.\d+\.\d+\.\d+", output)
            if match:
                major = int(match.group(1))
                print(f"  🧩 检测到本机 Chrome 主版本: {major}")
                return major
        except Exception as e:
            print(f"  ⚠️ 检测 Chrome 版本失败 ({binary}): {e}")
    print("  ℹ️ 未检测到本机 Chrome 版本，交由 undetected-chromedriver 自动匹配")
    return None


def apply_proxy_to_options(options: uc.ChromeOptions, proxy: dict | None) -> None:
    """
    将代理配置写入 ChromeOptions。
    proxy 格式:
      {
        "enabled": True,
        "type": "http" | "socks5",
        "host": "1.2.3.4",
        "port": 8080,
        "use_auth": False,
        "username": "",
        "password": ""
      }
    """
    if not proxy or not proxy.get("enabled"):
        return

    ptype = proxy.get("type", "http").lower()  # "http" | "socks5"
    host = proxy.get("host", "").strip()
    port = int(proxy.get("port", 0))
    use_auth = proxy.get("use_auth", False)
    username = proxy.get("username", "")
    password = proxy.get("password", "")

    if not host or not port:
        print("  ⚠️ 代理已启用但 host/port 无效，已跳过代理设置")
        return

    if ptype == "socks5":
        if use_auth and username:
            # Chrome 不支持 --proxy-server 内嵌 SOCKS5 凭证，也无法用扩展拦截 SOCKS5 握手。
            # 方案：启动本地无认证中继，由中继向上游注入凭证。
            local_port = _socks5_relay.start(host, port, username, password)
            options.add_argument(f"--proxy-server=socks5://127.0.0.1:{local_port}")
            print(f"  🔒 SOCKS5 代理（含认证，中继端口 {local_port}）: {host}:{port}")
        else:
            options.add_argument(f"--proxy-server=socks5://{host}:{port}")
            print(f"  🌐 SOCKS5 代理: socks5://{host}:{port}")
    else:
        # HTTP：无凭证直接用参数，有凭证需要扩展（Chrome 不支持 --proxy-server 内嵌 HTTP 凭证）
        if use_auth and username:
            ext_path = _build_proxy_extension("http", host, port, username, password)
            options.add_extension(ext_path)
            print(f"  🔒 HTTP 代理（含认证）: http://{host}:{port}")
        else:
            options.add_argument(f"--proxy-server=http://{host}:{port}")
            print(f"  🌐 HTTP 代理: http://{host}:{port}")


def create_driver(headless=False, proxy=None):
    """
    创建 undetected Chrome 浏览器驱动

    参数:
        headless (bool): 是否使用无头模式

    返回:
        uc.Chrome: 浏览器驱动实例
    """
    print(f"🌐 正在初始化浏览器 (Headless: {headless})...")
    options = uc.ChromeOptions()

    # === 伪无头模式 (Fake Headless) ===
    # 真正的 Headless 很难过 Cloudflare，我们使用"移出屏幕"的策略
    # 这样既拥有完整的浏览器指纹，用户又看不到窗口
    real_headless = False

    if headless:
        print("  👻 使用'伪无头'模式 (Off-screen) 以绕过检测...")
        options.add_argument("--window-position=-10000,-10000")
        options.add_argument("--window-size=1920,1080")
        options.add_argument(
            "--start-maximized"
        )  # 可能会覆盖 position，但在多屏下通常有效
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")

        # 仍然可以加一些伪装，虽然不是必需的，因为已经是真浏览器了
        options.add_argument("--lang=zh-CN,zh;q=0.9,en;q=0.8")

    # 应用代理设置
    apply_proxy_to_options(options, proxy)

    chrome_major_version = _detect_chrome_major_version()

    # 每个实例使用独立的临时用户目录：避免 undetected-chromedriver 默认共享 profile
    # 导致"上一个实例把当前实例的浏览器杀掉 / session deleted"这类不稳定问题
    try:
        profile_dir = tempfile.mkdtemp(prefix="uc-profile-")
        print(f"  🗂️ 独立浏览器配置目录: {profile_dir}")
    except Exception:
        profile_dir = None

    chrome_kwargs = {
        "options": options,
        "use_subprocess": True,
        "headless": real_headless,
    }
    if profile_dir:
        chrome_kwargs["user_data_dir"] = profile_dir
    if chrome_major_version is not None:
        chrome_kwargs["version_main"] = chrome_major_version

    # 使用自定义的 SafeChrome (注意: 传入 real_headless=False)
    driver = SafeChrome(**chrome_kwargs)

    # === 深度伪装 (针对 Headless 模式) ===
    if headless:
        print("🎭 应用深度指纹伪装...")

        # 1. 伪造 WebGL 供应商 (让它看起来像有真实显卡)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    // 37445: UNMASKED_VENDOR_WEBGL
                    // 37446: UNMASKED_RENDERER_WEBGL
                    if (parameter === 37445) {
                        return 'Intel Inc.';
                    }
                    if (parameter === 37446) {
                        return 'Intel(R) Iris(R) Xe Graphics';
                    }
                    return getParameter(parameter);
                };
            """
            },
        )

        # 2. 伪造插件列表 (Headless 默认是空的)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en'],
                });
            """
            },
        )

        # 3. 绕过常见的检测属性
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                // 覆盖 window.chrome
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };
                
                // 伪造 permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: 'denied' }) :
                    originalQuery(parameters)
                );
            """
            },
        )

    return driver


def log_browser_egress_ip(driver, timeout=12):
    """打印浏览器当前出口 IP，用于确认代理是否生效。"""
    print("🌍 正在检测浏览器出口 IP...")
    # 先尝试 fetch；某些环境会因为 about:blank/CORS 或代理握手导致 Failed to fetch
    try:
        driver.set_script_timeout(timeout)
        result = driver.execute_async_script(
            """
            const done = arguments[0];
            fetch('https://api.ipify.org?format=json', { cache: 'no-store' })
              .then(r => r.json())
              .then(d => done(d && d.ip ? String(d.ip) : ''))
              .catch(e => done('ERR:' + (e && e.message ? e.message : 'fetch_failed')));
            """
        )
        value = str(result or "").strip()
        if value and not value.startswith("ERR:"):
            print(f"  ✅ 浏览器出口 IP: {value}")
            return
        print(f"  ℹ️ fetch 检测未成功，准备回退页面检测: {value or 'empty_response'}")
    except Exception as e:
        print(f"  ℹ️ fetch 检测异常，准备回退页面检测: {e}")

    # 回退：直接导航到 IP 服务页面读取 body 文本（不依赖 CORS）
    endpoints = [
        "https://api.ipify.org",
        "https://api64.ipify.org",
        "https://ifconfig.me/ip",
    ]
    try:
        driver.set_page_load_timeout(timeout)
    except Exception:
        pass

    for url in endpoints:
        try:
            driver.get(url)
            body_text = driver.execute_script(
                "return (document.body && document.body.innerText) ? document.body.innerText : '';"
            )
            ip = (
                str(body_text or "").strip().splitlines()[0].strip()
                if body_text
                else ""
            )
            if ip and ("." in ip or ":" in ip):
                print(f"  ✅ 浏览器出口 IP: {ip} ({url})")
                return
        except Exception as e:
            print(f"  ⚠️ 回退检测失败 ({url}): {e}")

    print("  ❌ 出口 IP 检测失败：代理可能不可用，或当前网络阻断了 IP 检测服务")


def _sleep_with_heartbeat(
    driver, seconds, monitor_callback=None, step_name="heartbeat", interval=1.0
):
    """
    分段睡眠，期间定期上报监控，避免前端画面在长等待中“卡住”。
    """
    end_time = time.time() + max(0, float(seconds))
    while time.time() < end_time:
        if monitor_callback:
            monitor_callback(driver, step_name)
        remaining = end_time - time.time()
        time.sleep(min(interval, max(0.05, remaining)))


def check_and_handle_error(driver, max_retries=None, monitor_callback=None):
    """
    检测页面错误并自动重试

    参数:
        driver: 浏览器驱动
        max_retries: 最大重试次数

    返回:
        bool: 是否检测到错误并处理
    """
    if max_retries is None:
        max_retries = ERROR_PAGE_MAX_RETRIES

    for attempt in range(max_retries):
        try:
            page_source = driver.page_source.lower()
            error_keywords = [
                "出错",
                "error",
                "timed out",
                "operation timeout",
                "route error",
                "invalid content",
            ]
            has_error = any(keyword in page_source for keyword in error_keywords)

            if has_error:
                try:
                    retry_btn = driver.find_element(
                        By.CSS_SELECTOR, 'button[data-dd-action-name="Try again"]'
                    )
                    print(
                        f"⚠️ 检测到错误页面，正在重试（第 {attempt + 1}/{max_retries} 次）..."
                    )
                    driver.execute_script("arguments[0].click();", retry_btn)
                    wait_time = 5 + (attempt * 2)
                    print(f"  等待 {wait_time} 秒后继续...")
                    _sleep_with_heartbeat(
                        driver,
                        wait_time,
                        monitor_callback=monitor_callback,
                        step_name=f"error_retry_{attempt + 1}",
                    )
                    return True
                except Exception:
                    _sleep_with_heartbeat(
                        driver,
                        2,
                        monitor_callback=monitor_callback,
                        step_name="error_retry_backoff",
                    )
                    continue
            return False

        except Exception as e:
            print(f"  错误检测异常: {e}")
            return False

    return False


def click_button_with_retry(driver, selector, max_retries=None, monitor_callback=None):
    """
    带重试机制的按钮点击

    参数:
        driver: 浏览器驱动
        selector: CSS 选择器
        max_retries: 最大重试次数

    返回:
        bool: 是否成功点击
    """
    if max_retries is None:
        max_retries = BUTTON_CLICK_MAX_RETRIES

    for attempt in range(max_retries):
        try:
            button = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            driver.execute_script("arguments[0].click();", button)
            return True
        except Exception:
            print(f"  第 {attempt + 1} 次点击失败，正在重试...")
            _sleep_with_heartbeat(
                driver,
                2,
                monitor_callback=monitor_callback,
                step_name="button_retry_backoff",
            )

    return False


def type_slowly(element, text, delay=0.05):
    """
    模拟人工缓慢输入

    参数:
        element: 输入框元素
        text: 要输入的文本
        delay: 每个字符之间的延迟（秒）
    """
    for char in text:
        element.send_keys(char)
        time.sleep(delay)


def fill_signup_form(driver, email: str, password: str, monitor_callback=None):
    """
    填写注册表单
    适配 ChatGPT 新版统一登录/注册页面

    参数:
        driver: 浏览器驱动
        email: 邮箱地址
        password: 密码

    返回:
        tuple: (是否成功, 是否已输入密码)
    """
    wait = WebDriverWait(driver, MAX_WAIT_TIME)

    try:
        # 1. 等待邮箱输入框出现
        print(f"DEBUG: 当前页面标题: {driver.title}")
        print(f"DEBUG: 当前页面URL: {driver.current_url}")
        print("📧 等待邮箱输入框...")

        # 检查是否是 Cloudflare 验证页
        if (
            "Just a moment" in driver.title
            or "Ray ID" in driver.page_source
            or "请稍候" in driver.title
        ):
            print("⚠️ 检测到 Cloudflare 验证页面...")
            time.sleep(10)
            if "Just a moment" in driver.title or "请稍候" in driver.title:
                print("  🔄 尝试刷新页面以突破验证...")
                driver.refresh()
                time.sleep(10)

            try:
                frames = driver.find_elements(By.TAG_NAME, "iframe")
                for frame in frames:
                    try:
                        driver.switch_to.frame(frame)
                        checkbox = driver.find_elements(
                            By.CSS_SELECTOR,
                            "#checkbox, .checkbox, input[type='checkbox'], #challenge-stage",
                        )
                        if checkbox:
                            print("  🖱️ 尝试点击验证框...")
                            driver.execute_script("arguments[0].click();", checkbox[0])
                            time.sleep(5)
                        driver.switch_to.default_content()
                    except Exception:
                        driver.switch_to.default_content()
            except Exception:
                pass

        print("🔍 检查是否需要点击 注册/登录 按钮...")
        try:
            signup_btns = driver.find_elements(
                By.XPATH,
                '//button[contains(., "Sign up")] | //button[contains(., "注册")] | //div[contains(text(), "Sign up")] | //div[contains(text(), "注册")]',
            )
            login_btns = driver.find_elements(
                By.XPATH,
                '//button[contains(., "Log in")] | //button[contains(., "登录")] | //div[contains(text(), "Log in")] | //div[contains(text(), "登录")]',
            )

            target_btn = None
            if signup_btns:
                target_btn = signup_btns[0]
                print("  -> 找到 注册(Sign up) 按钮")
            elif login_btns:
                target_btn = login_btns[0]
                print("  -> 找到 登录(Log in) 按钮")

            if target_btn and target_btn.is_displayed():
                driver.execute_script("arguments[0].click();", target_btn)
                print("  ✅ 已点击入口按钮")
                time.sleep(3)
        except Exception as e:
            print(f"  ⚠️ 检查入口按钮时出错 (非致命): {e}")

        email_input = WebDriverWait(driver, SHORT_WAIT_TIME).until(
            EC.visibility_of_element_located(
                (
                    By.CSS_SELECTOR,
                    'input[type="email"], input[name="email"], input[autocomplete="email"]',
                )
            )
        )

        print("📝 正在输入邮箱...")
        actions = ActionChains(driver)
        actions.move_to_element(email_input)
        actions.click()
        actions.pause(0.3)
        actions.send_keys(email)
        actions.perform()

        time.sleep(1)
        actual_value = email_input.get_attribute("value")
        if actual_value == email:
            print(f"✅ 已输入邮箱: {email}")
        else:
            print(f"⚠️ 输入可能不完整，实际值: {actual_value}")

        time.sleep(1)

        print("🔘 点击继续按钮...")
        continue_btn = _find_email_submit_button(driver, email_input)
        if continue_btn is None:
            print("❌ 未找到邮箱提交按钮（继续）")
            dump_page_diagnostics(driver, "email_submit_button_missing")
            return False, False
        _click_and_confirm_submit(driver, continue_btn, email_input)
        print("✅ 已点击继续")
        time.sleep(2)

        # 提交邮箱后，官方可能有网络延迟/CF 质询，给足等待时间再判定
        post_email_timeout = int(os.environ.get("GPT_POST_EMAIL_TIMEOUT", "45"))
        next_step = _wait_for_post_email_step(driver, timeout=post_email_timeout)
        if next_step == "already_registered":
            return "already_registered", False
        if next_step == "verification":
            # 官方把注册做成了"先邮箱码验证、后设密码"两段式。
            # 验证码页上有「使用密码继续」链接（a[href*='/create-account/password']），
            # 点击它切换到密码创建页 → 账号即带密码（否则账号免密，登录只能靠邮箱码）。
            print("🔀 官方进入邮箱验证码分支，尝试切换到「使用密码继续」以带上密码 ...")
            if _switch_to_password_signup(driver, log=print):
                next_step = _wait_for_post_email_step(driver, timeout=post_email_timeout)
                print(f"   ↪️ 切换后页面状态: {next_step} | URL: {driver.current_url}")
                if next_step == "password":
                    print("  ✅ 成功切换到密码创建页，账号将带密码")
                elif next_step == "verification":
                    print("  ℹ️ 官方仍要求验证码优先，本轮账号为免密（后续可在设置页补密码）")
                    return True, False
                elif next_step == "already_registered":
                    return "already_registered", False
            else:
                print("  ℹ️ 未能切换到密码页，本轮账号为免密注册")
                return True, False
        if next_step != "password":
            if check_email_already_registered(driver):
                return "already_registered", False
            # 再给一次机会：可能是点击未生效或页面仍在加载
            try:
                print("🔁 未识别到下一步页面，重试点击【继续】并再等待一次...")
                retry_btn = _find_email_submit_button(driver, email_input, force=True)
                if retry_btn is None:
                    retry_btn = WebDriverWait(driver, 10).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[type="submit"]'))
                    )
                _click_and_confirm_submit(driver, retry_btn, email_input)
                time.sleep(2)
                next_step = _wait_for_post_email_step(driver, timeout=post_email_timeout)
                if next_step == "already_registered":
                    return "already_registered", False
                if next_step == "verification":
                    return True, False
            except Exception as retry_exc:
                print(f"   ↳ 重试失败: {retry_exc}")
            if next_step != "password":
                return False, False

        print("🔑 等待密码输入框...")
        password_input = WebDriverWait(driver, SHORT_WAIT_TIME).until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, 'input[autocomplete="new-password"], input[type="password"]')
            )
        )
        password_input.clear()
        time.sleep(0.5)
        type_slowly(password_input, password)
        print("✅ 已输入密码")
        time.sleep(2)

        print("🔘 点击继续按钮...")
        if not click_button_with_retry(
            driver, 'button[type="submit"]', monitor_callback=monitor_callback
        ):
            print("❌ 点击继续按钮失败")
            return False, False
        print("✅ 已点击继续")

        time.sleep(3)
        if check_email_already_registered(driver):
            return "already_registered", True

        while check_and_handle_error(driver, monitor_callback=monitor_callback):
            if check_email_already_registered(driver):
                return "already_registered", True
            _sleep_with_heartbeat(
                driver,
                2,
                monitor_callback=monitor_callback,
                step_name="error_recheck_wait",
            )

        return True, True

    except Exception as e:
        print(f"❌ 填写表单失败: {e}")
        return False, False


def login(driver, email, password):
    """
    登录 ChatGPT
    """
    print(f"🔐 正在登录 {email}...")
    wait = WebDriverWait(driver, 30)

    try:
        driver.get("https://chat.openai.com/auth/login")
        time.sleep(5)

        # 0. 点击初始页面的 Log in / 登录 按钮
        print("🔘 寻找 Log in / 登录 按钮...")
        try:
            # 尝试多种选择器，支持中文
            xpaths = [
                '//button[@data-testid="login-button"]',
                '//button[contains(., "Log in")]',
                '//button[contains(., "登录")]',
                '//div[contains(text(), "Log in")]',
                '//div[contains(text(), "登录")]',
            ]

            login_btn = None
            for xpath in xpaths:
                try:
                    btns = driver.find_elements(By.XPATH, xpath)
                    for btn in btns:
                        if btn.is_displayed():
                            login_btn = btn
                            break
                    if login_btn:
                        break
                except Exception:
                    continue

            if login_btn:
                # 确保点击
                try:
                    login_btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", login_btn)
                print("✅ 点击了登录按钮")
            else:
                print("⚠️ 未找到显式的登录按钮，尝试直接寻找输入框")
        except Exception as e:
            print(f"⚠️ 点击登录按钮出错: {e}")

        time.sleep(3)

        # 1. 输入邮箱
        print("📧 输入邮箱...")
        # 增加等待时间
        email_input = wait.until(
            EC.visibility_of_element_located(
                (
                    By.CSS_SELECTOR,
                    'input[name="username"], input[name="email"], input[id="email-input"]',
                )
            )
        )
        email_input.clear()
        type_slowly(email_input, email)

        # 点击继续
        print("🔘 点击继续...")
        continue_btn = driver.find_element(
            By.CSS_SELECTOR, 'button[type="submit"], button[class*="continue-btn"]'
        )
        continue_btn.click()
        time.sleep(3)

        # ⚠️ 关键修正：检查是否进入了验证码模式，如果是，切换回密码模式
        print("🔍 检查登录方式...")
        try:
            # 寻找所有包含 "密码" 或 "Password" 的文本元素，只要它们看起来像链接或按钮
            # 排除掉密码输入框本身的 label
            switch_candidates = driver.find_elements(
                By.XPATH,
                '//*[contains(text(), "密码") or contains(text(), "Password")]',
            )

            clicked_switch = False
            for el in switch_candidates:
                if not el.is_displayed():
                    continue

                tag_name = el.tag_name.lower()
                text = el.text

                # 排除 label 和 title
                if (
                    tag_name in ["h1", "h2", "label", "span"]
                    and "输入" not in text
                    and "Enter" not in text
                    and "使用" not in text
                ):
                    continue

                # 尝试点击看起来像切换链接的元素
                if (
                    "输入密码" in text
                    or "Enter password" in text
                    or "使用密码" in text
                    or "password instead" in text
                ):
                    print(f"⚠️ 尝试点击切换链接: '{text}' ({tag_name})...")
                    try:
                        el.click()
                        clicked_switch = True
                        time.sleep(2)
                        break
                    except Exception:
                        # 可能是被遮挡，尝试 JS 点击
                        driver.execute_script("arguments[0].click();", el)
                        clicked_switch = True
                        time.sleep(2)
                        break

            if not clicked_switch:
                print("  ℹ️ 未找到明显的'切换密码'链接，假设在密码输入页或强制验证码页")

        except Exception as e:
            print(f"  检查登录方式出错: {e}")

        # 2. 输入密码
        print("🔑 等待密码输入框...")
        try:
            password_input = wait.until(
                EC.visibility_of_element_located(
                    (By.CSS_SELECTOR, 'input[name="password"], input[type="password"]')
                )
            )
            password_input.clear()
            type_slowly(password_input, password)

            # 点击继续/登录
            print("🔘 点击登录...")
            continue_btn = driver.find_element(
                By.CSS_SELECTOR, 'button[type="submit"], button[name="action"]'
            )
            continue_btn.click()

            print("⏳ 等待登录完成...")
            time.sleep(10)

        except Exception as e:
            print("❌ 未找到密码输入框。")
            print("  可能原因: 1. 强制验证码登录; 2. 页面加载过慢; 3. 选择器失效")
            print("  尝试手动干预或检查页面...")
            raise e  # 抛出异常以终止测试

        # 检查是否登录成功
        if "auth" not in driver.current_url:
            print("✅ 登录成功")
            return True
        else:
            print("⚠️ 可能还在登录页面 (URL包含 auth)")
            # 再次检查是否有错误提示
            try:
                err = driver.find_element(
                    By.CSS_SELECTOR, '.error-message, [role="alert"]'
                )
                print(f"❌登录错误提示: {err.text}")
            except Exception:
                pass
            return True

    except Exception as e:
        print(f"❌ 登录失败: {e}")
        return False


def _find_verification_inputs(driver, code: str):
    selectors = [
        'input[name="code"]',
        'input[name*="code"]',
        'input[autocomplete="one-time-code"]',
        'input[placeholder*="代码"]',
        'input[placeholder*="验证码"]',
        'input[placeholder*="code" i]',
        'input[placeholder*="verification" i]',
        'input[aria-label*="代码"]',
        'input[aria-label*="验证码"]',
        'input[aria-label*="code" i]',
        'input[aria-label*="verification" i]',
    ]
    for selector in selectors:
        try:
            elements = [
                el for el in driver.find_elements(By.CSS_SELECTOR, selector)
                if el.is_displayed() and el.is_enabled()
            ]
            if elements:
                return "single", elements
        except Exception:
            continue

    try:
        otp_candidates = [
            el for el in driver.find_elements(
                By.CSS_SELECTOR,
                'input[inputmode="numeric"], input[autocomplete="one-time-code"], input[maxlength="1"]',
            )
            if el.is_displayed() and el.is_enabled()
        ]
        if len(otp_candidates) >= 4:
            otp_candidates = sorted(
                otp_candidates,
                key=lambda el: (el.location.get("y", 0), el.location.get("x", 0)),
            )
            return "multi", otp_candidates[: len(code)]
    except Exception:
        pass

    return None, []


def enter_verification_code(driver, code: str, monitor_callback=None):
    """
    输入验证码

    参数:
        driver: 浏览器驱动
        code: 验证码

    返回:
        bool: 是否成功
    """
    try:
        print("🔢 正在输入验证码...")

        while check_and_handle_error(driver, monitor_callback=monitor_callback):
            _sleep_with_heartbeat(
                driver,
                2,
                monitor_callback=monitor_callback,
                step_name="code_error_recheck_wait",
            )

        input_mode = None
        input_elements = []
        end_time = time.time() + 60
        while time.time() < end_time:
            input_mode, input_elements = _find_verification_inputs(driver, code)
            if input_elements:
                break
            time.sleep(0.5)

        if not input_elements:
            raise RuntimeError("未找到验证码输入框")

        if input_mode == "single":
            code_input = input_elements[0]
            code_input.clear()
            time.sleep(0.5)
            type_slowly(code_input, code, delay=0.1)
        else:
            for idx, digit in enumerate(code):
                if idx >= len(input_elements):
                    break
                box = input_elements[idx]
                try:
                    box.clear()
                except Exception:
                    pass
                box.click()
                time.sleep(0.1)
                box.send_keys(digit)

        print(f"✅ 已输入验证码: {code}")
        time.sleep(2)

        print("🔘 点击继续按钮...")
        if not click_button_with_retry(
            driver, 'button[type="submit"]', monitor_callback=monitor_callback
        ):
            print("❌ 点击继续按钮失败")
            return False
        print("✅ 已点击继续")

        time.sleep(3)
        while check_and_handle_error(driver, monitor_callback=monitor_callback):
            _sleep_with_heartbeat(
                driver,
                2,
                monitor_callback=monitor_callback,
                step_name="code_submit_error_recheck_wait",
            )

        return True

    except Exception as e:
        print(f"❌ 输入验证码失败: {e}")
        return False


def _set_input_value(driver, element, value) -> bool:
    """往受控输入框写值，绕过 React Aria 的 label 遮挡导致的 click 被拦截。

    依次尝试：原生 setter + input/change 事件 → JS focus 后 send_keys → Actions 点击后输入。
    """
    target = str(value)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        time.sleep(0.2)
    except Exception:
        pass

    try:
        driver.execute_script(
            """
            const el = arguments[0], value = arguments[1];
            const proto = (el instanceof HTMLTextAreaElement)
                ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
            """,
            element, target,
        )
        if (element.get_attribute("value") or "").strip() == target:
            return True
    except Exception as exc:
        print(f"   ⚠️ 原生赋值失败: {str(exc)[:100]}")

    try:
        driver.execute_script("arguments[0].focus();", element)
        element.send_keys(Keys.CONTROL + "a")
        element.send_keys(target)
        if (element.get_attribute("value") or "").strip():
            return True
    except Exception as exc:
        print(f"   ⚠️ 聚焦输入失败: {str(exc)[:100]}")

    try:
        actions = ActionChains(driver)
        actions.move_to_element(element).click().send_keys(target).perform()
        if (element.get_attribute("value") or "").strip():
            return True
    except Exception as exc:
        print(f"   ⚠️ 动作链输入失败: {str(exc)[:100]}")

    return False


def _submit_and_wait_progress(driver, button, previous_url: str, timeout: float = 12.0) -> bool:
    """点击提交并确认页面真的推进了（URL 变化或提交按钮消失）。"""
    attempts = (
        ("原生点击", lambda: button.click() if button is not None else None),
        ("JS 点击", lambda: driver.execute_script("arguments[0].click();", button) if button is not None else None),
        ("回车提交", lambda: driver.switch_to.active_element.send_keys(Keys.ENTER)),
    )
    for index, (name, action) in enumerate(attempts):
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            print(f"   ⚠️ {name}失败: {str(exc)[:120]}")
            continue
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                current_url = str(driver.current_url or "")
            except Exception:
                current_url = previous_url
            if current_url != previous_url:
                print(f"   ✅ 页面已推进: {current_url}（{name} 生效）")
                return True
            try:
                if button is not None and not button.is_displayed():
                    print(f"   ✅ 提交按钮已消失（{name} 生效）")
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        if index < len(attempts) - 1:
            print(f"   ↪️ {name} 后页面无变化，改用下一种提交方式...")
    return False


def fill_profile_info(driver):
    """
    填写用户资料（随机生成的姓名和生日/年龄）

    参数:
        driver: 浏览器驱动

    返回:
        bool: 是否成功（必须确认提交后页面真的推进，否则返回 False）
    """
    wait = WebDriverWait(driver, MAX_WAIT_TIME)

    # 生成随机用户信息
    user_info = generate_user_info()
    user_name = user_info["name"]
    birthday_year = user_info["year"]
    birthday_month = user_info["month"]
    birthday_day = user_info["day"]

    try:
        profile_url = str(driver.current_url or "")

        # 1. 输入姓名（React Aria 受控输入统一走原生赋值）
        print("👤 等待姓名输入框...")
        name_input = WebDriverWait(driver, 60).until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, 'input[name="name"], input[autocomplete="name"]')
            )
        )
        if _set_input_value(driver, name_input, user_name):
            print(f"✅ 已输入姓名: {user_name}")
        else:
            name_input.clear()
            type_slowly(name_input, user_name)
            print(f"✅ 已输入姓名(键盘回退): {user_name}")
        time.sleep(1)

        # 2. 输入年龄或生日（必须真的写进去，否则官方会拒绝提交）
        print("🎂 正在检查并输入年龄或生日...")
        time.sleep(1)

        age_inputs = driver.find_elements(
            By.CSS_SELECTOR,
            'input[name="age"], input[type="number"], input[id*="age"], input[placeholder*="年龄" i], input[placeholder*="Age" i]'
        )
        age_filled = False
        for a_inp in age_inputs:
            try:
                if not (a_inp.is_displayed() and a_inp.is_enabled()):
                    continue
            except Exception:
                continue
            if _set_input_value(driver, a_inp, "24"):
                print(f"✅ 已输入年龄: 24 (实际值: {a_inp.get_attribute('value')!r})")
                age_filled = True
                break
            print(f"  ⚠️ 年龄输入框写入未生效，尝试下一个候选")

        if not age_filled and age_inputs:
            print("  ❌ 年龄输入框存在但写入失败，输出页面诊断")
            dump_page_diagnostics(driver, "age_input_failed")
            return False

        # 若非年龄输入，回退至年份/月份/日期三段式
        if not age_filled:
            try:
                year_inputs = driver.find_elements(By.CSS_SELECTOR, '[data-type="year"]')
                if year_inputs and year_inputs[0].is_displayed():
                    year_input = year_inputs[0]
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", year_input)
                    time.sleep(0.3)
                    actions = ActionChains(driver)
                    actions.click(year_input).perform()
                    time.sleep(0.2)
                    year_input.send_keys(Keys.CONTROL + "a")
                    time.sleep(0.1)
                    type_slowly(year_input, birthday_year, delay=0.1)

                    month_input = driver.find_element(By.CSS_SELECTOR, '[data-type="month"]')
                    actions = ActionChains(driver)
                    actions.click(month_input).perform()
                    time.sleep(0.2)
                    month_input.send_keys(Keys.CONTROL + "a")
                    time.sleep(0.1)
                    type_slowly(month_input, birthday_month, delay=0.1)

                    day_input = driver.find_element(By.CSS_SELECTOR, '[data-type="day"]')
                    actions = ActionChains(driver)
                    actions.click(day_input).perform()
                    time.sleep(0.2)
                    day_input.send_keys(Keys.CONTROL + "a")
                    time.sleep(0.1)
                    type_slowly(day_input, birthday_day, delay=0.1)
                    print(f"✅ 已输入生日: {birthday_year}/{birthday_month}/{birthday_day}")
            except Exception as e_bday:
                print(f"  ℹ️ 生日输入尝试 (可选): {e_bday}")

        time.sleep(1)

        # 3. 提交资料，并确认页面真的推进（否则官方校验未通过）
        print("🔘 点击最终提交按钮...")
        submit_xpath = (
            "//button[contains(normalize-space(.), '继续') or contains(normalize-space(.), 'Continue') "
            "or contains(normalize-space(.), '完成') or contains(normalize-space(.), 'Finish') "
            "or contains(normalize-space(.), 'Agree') or contains(normalize-space(.), '同意')]"
        )
        submit_button = None
        for selector in ('button[type="submit"]', 'button[data-testid*="submit"]', submit_xpath):
            try:
                found = (
                    driver.find_elements(By.XPATH, selector) if selector.startswith("//")
                    else driver.find_elements(By.CSS_SELECTOR, selector)
                )
            except Exception:
                continue
            visible = [element for element in found if element.is_displayed() and element.is_enabled()]
            if visible:
                submit_button = visible[-1]
                break

        advanced = _submit_and_wait_progress(driver, submit_button, profile_url, timeout=15)

        if not advanced:
            # 常见原因：年龄/生日没写进去。重新填一次再提交
            print("🔁 资料页未推进，重新填写年龄/生日后重试提交...")
            for a_inp in driver.find_elements(
                By.CSS_SELECTOR,
                'input[name="age"], input[type="number"], input[placeholder*="年龄" i], input[placeholder*="Age" i]',
            ):
                try:
                    if a_inp.is_displayed() and a_inp.is_enabled():
                        _set_input_value(driver, a_inp, "24")
                except Exception:
                    continue
            try:
                retry_button = driver.find_elements(By.CSS_SELECTOR, 'button[type="submit"]')
                advanced = _submit_and_wait_progress(
                    driver,
                    next((b for b in retry_button if b.is_displayed() and b.is_enabled()), None),
                    profile_url,
                    timeout=15,
                )
            except Exception as retry_exc:
                print(f"   ↳ 重试提交失败: {retry_exc}")

        if not advanced:
            error_text = _visible_error_text(driver)
            print(f"❌ 资料提交后页面未推进{'；官方提示: ' + error_text if error_text else ''}")
            dump_page_diagnostics(driver, "profile_submit_failed")
            return False

        print("✅ 资料已提交并确认推进")
        return True

    except Exception as e:
        print(f"❌ 填写资料失败: {e}")
        return False



def _dismiss_onboarding(driver):
    """尝试关闭 onboarding 弹窗（条款确认、功能介绍等）。"""
    try:
        # 查找并点击 "Continue" / "Next" / "Done" / "Start" / "OK" 类按钮
        dismiss_selectors = [
            'button[data-testid="onboarding-continue-button"]',
            'button[data-testid="close-onboarding-button"]',
            'button[class*="onboarding"]',
        ]
        for sel in dismiss_selectors:
            try:
                btns = driver.find_elements(By.CSS_SELECTOR, sel)
                for btn in btns:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        print("  📌 点击了 onboarding 按钮")
                        time.sleep(2)
                        return True
            except Exception:
                continue

        # 更通用的方式：找包含 Continue/Next/Done/Start/OK 的按钮
        try:
            generic_btns = driver.find_elements(
                By.XPATH,
                '//button[contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "continue") '
                'or contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "next") '
                'or contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "done") '
                'or contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "start") '
                'or contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "ok") '
                'or contains(text(), "继续") '
                'or contains(text(), "完成") '
                'or contains(text(), "开始")]'
            )
            for btn in generic_btns:
                if btn.is_displayed():
                    driver.execute_script("arguments[0].click();", btn)
                    print("  📌 点击了 onboarding/引导按钮")
                    time.sleep(2)
                    return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def verify_logged_in(driver, timeout=90):
    """
    验证当前浏览器会话是否已成功登录 ChatGPT

    参数:
        driver: 浏览器驱动
        timeout: 最大等待时长（秒）

    返回:
        bool: 是否验证成功
    """
    print("🔍 正在验证登录状态...")

    # 先等待页面跳转完成；注册成功后有时会在 onboarding 页停留数秒
    end_time = time.time() + timeout
    logged_in_selectors = [
        "textarea#prompt-textarea",
        'button[data-testid="profile-button"]',
        'button[data-testid="user-menu-button"]',
        'nav a[href*="/settings"]',
        'a[href*="/settings"]',
    ]

    while time.time() < end_time:
        try:
            current_url = (driver.current_url or "").lower()

            # 若仍处于认证路径，继续等待跳转
            if any(key in current_url for key in ["/auth", "login", "signup"]):
                time.sleep(2)
                continue

            # 出现核心已登录元素即可判定成功
            for selector in logged_in_selectors:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if any(el.is_displayed() for el in elements):
                    print("✅ 登录状态验证成功")
                    return True

            page_source = (driver.page_source or "").lower()
            blocked_markers = [
                "verify your email",
                "enter code",
                "log in",
                "sign up",
            ]

            if any(marker in page_source for marker in blocked_markers):
                time.sleep(2)
                continue

            # 尝试关闭 onboarding 弹窗
            _dismiss_onboarding(driver)

            # 如果 URL 已不在 auth 路径，且没有明显登录/注册提示，作为兜底判定
            if "auth" not in current_url:
                print("✅ 登录状态验证成功（URL 兜底判定）")
                return True

        except Exception as e:
            print(f"  登录状态检查中断: {e}")

        time.sleep(2)

    print("❌ 登录状态验证失败：超时仍未确认登录")
    return False
