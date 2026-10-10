"""
ChatGPT 账号自动注册 - 主程序（浏览器自动化）
使用临时邮箱完成注册流程，注册结束后立刻在同一个已登录会话里:
  1. 捕获真实 Web Session Token（/api/auth/session 的 accessToken + session cookie）
  2. 通过官方安全设置真实开启 2FA (TOTP) 并取回真实密钥
  3. 用官方 backend-api 采集真实的订阅状态 / 1 个月 Plus 试用资格 / 到期时间
所以批量注册跑完，面板里就是可以直接使用的账号-密码-动态2FA-注册时间-资格数据。
"""

import argparse
import json
import os
import time
import random
from datetime import datetime
from pathlib import Path

from .config import TOTAL_ACCOUNTS, BATCH_INTERVAL_MIN, BATCH_INTERVAL_MAX, cfg, PROJECT_ROOT
from .utils import generate_random_password, save_to_txt, update_account_status
from .account_checker import check_account_status
from .account_probe import probe_in_browser, probe_with_token
from . import email_providers
from .oauth_service import perform_codex_oauth_login, save_codex_tokens
from .browser import (
    create_driver,
    log_browser_egress_ip,
    fill_signup_form,
    enter_verification_code,
    fill_profile_info,
    verify_logged_in,
)

# 在已登录页面上下文里取真实 Web 会话凭据（同源请求，自带 Cloudflare clearance）
BROWSER_SESSION_JS = r"""
return (async () => {
  try {
    const resp = await fetch('/api/auth/session', {
      credentials: 'include',
      headers: { 'accept': 'application/json' },
    });
    const text = await resp.text();
    let payload = null;
    try { payload = JSON.parse(text); } catch (e) { payload = null; }
    return { status: resp.status, payload: payload, text: (text || '').slice(0, 4000) };
  } catch (err) {
    return { status: 0, error: String(err), payload: null, text: '' };
  }
})()
"""


class RegisterResult(tuple):
    """包装注册结果元组，附加 skipped 标识以便统计跳过数量"""
    def __new__(cls, values, skipped=False):
        instance = super().__new__(cls, values)
        instance.skipped = skipped
        return instance


def _token_dir() -> Path:
    token_dir = Path(cfg.oauth.token_json_dir)
    if not token_dir.is_absolute():
        token_dir = PROJECT_ROOT / token_dir
    token_dir.mkdir(parents=True, exist_ok=True)
    return token_dir


def capture_web_session(driver, email: str, log=print) -> dict:
    """捕获真实 ChatGPT Web 凭据并落盘。

    Returns:
        {"access_token": str, "session_payload": dict|None, "cookie": str, "saved_file": str}
    """
    result = {"access_token": "", "session_payload": None, "cookie": "", "saved_file": ""}
    payload = None
    status = 0
    try:
        raw = driver.execute_script(BROWSER_SESSION_JS)
        if isinstance(raw, dict):
            status = raw.get("status", 0)
            payload = raw.get("payload")
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠️ 获取 Web 会话凭据失败: {exc}")

    if isinstance(payload, dict) and payload.get("accessToken"):
        result["access_token"] = payload.get("accessToken") or ""
        result["session_payload"] = payload

    # 保存全部 cookie（新版 NextAuth 会把 session token 分块，名字带 .0/.1 后缀）
    cookies = []
    try:
        cookies = driver.get_cookies() or []
    except Exception:
        cookies = []
    session_cookie_names = [
        cookie.get("name", "") for cookie in cookies
        if "session-token" in (cookie.get("name") or "").lower()
    ]
    if session_cookie_names:
        # 优先取无分块后缀的主 cookie，否则按名字排序拼接顺序
        main_name = next((n for n in session_cookie_names if not n[-2:-1] == "."), session_cookie_names[0])
        for cookie in cookies:
            if cookie.get("name") == main_name:
                result["cookie"] = cookie.get("value", "")
                break

    log(
        f"  🌐 ChatGPT Web 会话接口: HTTP {status} | accessToken: "
        f"{'已获取' if result['access_token'] else '未获取'} | cookie 数: {len(cookies)}"
        + (f" | session cookie: {', '.join(session_cookie_names)}" if session_cookie_names else "")
    )

    if result["access_token"] or cookies:
        try:
            token_file = _token_dir() / f"web-{email}.json"
            token_file.write_text(
                json.dumps(
                    {
                        "email": email,
                        "type": "web",
                        "access_token": result["access_token"],
                        "session_token": result["cookie"],
                        "session_cookie_names": session_cookie_names,
                        "cookies": cookies,
                        "expires": (payload or {}).get("expires", "") if isinstance(payload, dict) else "",
                        "account": (payload or {}).get("account") if isinstance(payload, dict) else None,
                        "user": (payload or {}).get("user") if isinstance(payload, dict) else None,
                        "saved_at": datetime.now().isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            result["saved_file"] = str(token_file)
            log(f"  💾 真实 Web 凭据已保存: {token_file.name}")
        except Exception as exc:  # noqa: BLE001
            log(f"  ⚠️ 保存 Web 凭据失败: {exc}")
    return result


def collect_real_state(driver, email: str, web_session: dict, proxy=None, log=print) -> dict:
    """在线采集真实状态：优先浏览器内页通道，失败再用 access token 走协议通道。"""
    state = probe_in_browser(driver, email=email, log=log)
    if not state.get("verified") and web_session.get("access_token"):
        log("  ℹ️ 浏览器内页通道未取得权益数据，回退到 Bearer 协议通道复核...")
        fallback = probe_with_token(
            web_session["access_token"], email=email, token_kind="web", proxy=proxy, timeout=12
        )
        if fallback.get("verified") or not state.get("sources"):
            fallback["evidence"]["fallback_from"] = "browser_probe"
            state = fallback
    return state


def register_one_account(
    monitor_callback=None,
    email_provider="mailtm",
    headless=False,
    proxy=None,
    referral_url: str | None = None,
    enable_2fa: bool | None = None,
):
    """
    注册单个账号（注册 → 真实 Web 凭证 → 真实 2FA → 真实权益检测）

    返回:
        tuple: (邮箱, 密码, 是否成功)
    """
    driver = None
    email = None
    password = None
    success = False
    temp_credential = None

    # 获取提供商信息
    provider_info = email_providers.get_provider_info(email_provider)
    if not provider_info:
        print(f"❌ 未知邮箱提供商: {email_provider}，回退到 mail.tm")
        email_provider = "mailtm"
        provider_info = email_providers.get_provider_info("mailtm")

    provider_name = provider_info["name"]

    def _report(step_name):
        if monitor_callback and driver:
            monitor_callback(driver, step_name)

    try:
        # 1. 创建临时邮箱
        print(f"📧 正在使用 {provider_name} 创建临时邮箱...")
        email, token, temp_credential = email_providers.create_temp_email(email_provider)
        if not email:
            print(f"❌ 创建邮箱失败（{provider_name}），终止注册")
            return None, None, False

        # 已注册过的邮箱直接跳过
        from .stored_accounts import get_registered_account_by_email, is_registered_status

        accounts_file_path = cfg.files.accounts_file
        if not os.path.isabs(accounts_file_path):
            accounts_file_path = str(PROJECT_ROOT / accounts_file_path)

        existing_acc = get_registered_account_by_email(accounts_file_path, email)
        if existing_acc and is_registered_status(existing_acc.get("status")):
            print(f"⏩ 邮箱 [{email}] 已在系统中成功注册过 GPT 账号，直接跳过注册流程（表格依然保留展示）")
            return RegisterResult((email, existing_acc.get("password"), True), skipped=True)

        if temp_credential:
            save_to_txt(
                email,
                password,
                "邮箱已创建",
                mailtm_password=str(temp_credential or ""),
                provider=email_provider,
            )

        # 2. 生成随机密码
        password = generate_random_password()

        # 3. 初始化浏览器
        driver = create_driver(headless=headless, proxy=proxy)
        _report("init_browser")

        if proxy and proxy.get("enabled"):
            log_browser_egress_ip(driver)
            _report("proxy_ip_check")

        # 4. 打开注册入口（邀请链接不构成试用资格证明，仅作为绑定入口）
        target_ref = (
            referral_url.strip()
            if referral_url and referral_url.strip()
            else getattr(cfg.registration, "referral_url", "").strip()
        )
        url = target_ref if target_ref else "https://chat.openai.com/chat"
        print(f"🌐 正在打开 {url}...")
        try:
            driver.get(url)
        except Exception as e:
            current_url = ""
            handle_count = 0
            try:
                current_url = str(driver.current_url or "")
            except Exception:
                pass
            try:
                handle_count = len(driver.window_handles)
            except Exception:
                pass
            raise RuntimeError(
                f"打开 ChatGPT 页面失败: {e} | current_url={current_url or 'N/A'} | windows={handle_count}"
            ) from e
        time.sleep(3)
        _report("open_page")

        # 5. 填写注册表单（邮箱和密码）
        existing_codes = email_providers.list_verification_codes(email_provider, str(token or ""))
        form_res, password_entered = fill_signup_form(
            driver, email, password, monitor_callback=monitor_callback
        )
        if form_res == "already_registered":
            print(f"⏩ 页面提示邮箱 [{email}] 已在 OpenAI 官方注册过 GPT 账号，跳过注册并在表格中保留展示")
            update_account_status(email, "已在官方注册(跳过)", password=password, provider=email_provider)
            return RegisterResult((email, password, True), skipped=True)
        if not form_res:
            from .browser import check_email_already_registered

            if check_email_already_registered(driver):
                print(f"⏩ 页面提示邮箱 [{email}] 已在 OpenAI 官方注册过 GPT 账号，跳过注册并在表格中保留展示")
                update_account_status(email, "已在官方注册(跳过)", password=password, provider=email_provider)
                return RegisterResult((email, password, True), skipped=True)
            print("❌ 填写注册表单失败")
            return email, password, False
        if not password_entered:
            print("ℹ️ 本次流程直接进入邮箱验证码页，未设置密码（后续由【完善账号】补设真实密码）")
            password = None
        _report("fill_form")

        # 6. 等待验证邮件
        time.sleep(3)
        safe_token = str(token or "")
        verification_code = email_providers.wait_for_verification_email(
            email_provider, safe_token, exclude_codes=existing_codes
        )
        if not verification_code and existing_codes:
            # 中断后重跑时，官方可能因频控不再发新码，此时收件箱里的旧码仍然有效
            fallback_code = existing_codes[0] if isinstance(existing_codes, list) else next(iter(existing_codes))
            print(f"↩️ 未收到新验证码，回退使用收件箱中已存在的验证码: {fallback_code}（多为上次中断留下的）")
            verification_code = fallback_code
        if not verification_code:
            print("❌ 未获取到验证码，终止注册")
            return email, password, False

        # 7. 输入验证码
        if not enter_verification_code(driver, verification_code, monitor_callback=monitor_callback):
            print("❌ 输入验证码失败")
            return email, password, False
        _report("enter_code")

        # 8. 填写个人资料
        if not fill_profile_info(driver):
            print("❌ 填写个人资料失败")
            return email, password, False
        _report("fill_profile")

        time.sleep(4)

        # 9. 最终校验：确认已登录
        if not verify_logged_in(driver):
            print("❌ 最终登录状态校验失败")
            return email, password, False
        _report("verify_logged_in")

        # 10. 捕获真实 Web Session Token（此前因缺少 json/datetime/Path 绑定而抛 NameError 被吞掉）
        web_session = capture_web_session(driver, email)
        _report("capture_session")

        # 11. 真实开启 2FA (TOTP)，取回官方真实 32 位密钥
        two_factor_secret = ""
        two_factor_note = ""
        two_factor_activated = False
        should_enable_2fa = enable_2fa if enable_2fa is not None else getattr(cfg.registration, "enable_2fa", True)
        if should_enable_2fa and driver:
            print("🔐 正在真实开启 2FA (TOTP 双重身份验证)...")
            try:
                from .two_factor_service import enable_account_2fa

                res_2fa = enable_account_2fa(
                    driver,
                    account_password=password or "",
                    log_func=print,
                    monitor_callback=monitor_callback,
                )
                two_factor_secret = res_2fa.get("secret", "") or ""
                two_factor_activated = bool(res_2fa.get("activated"))
                if res_2fa.get("success"):
                    print(f"  ✅ 2FA 已由官方确认生效! 32位密钥: {two_factor_secret}")
                elif two_factor_secret:
                    two_factor_note = str(res_2fa.get("error") or "secret_obtained_but_not_activated")
                    print(f"  ⚠️ 已取回官方真实密钥但未确认激活: {two_factor_secret}")
                else:
                    # 常见于瞬时 Cloudflare 403：此时仍在"新鲜认证"窗口内，重试一次可显著提高成功率
                    two_factor_note = str(res_2fa.get("error") or "2fa_not_verified")
                    print(f"  ⚠️ 2FA 首次未取到密钥 ({two_factor_note})，5 秒后重试一次...")
                    time.sleep(5)
                    res_retry = enable_account_2fa(
                        driver,
                        account_password=password or "",
                        log_func=print,
                        monitor_callback=monitor_callback,
                    )
                    if res_retry.get("secret"):
                        two_factor_secret = res_retry.get("secret", "") or ""
                        two_factor_activated = bool(res_retry.get("activated"))
                        two_factor_note = str(res_retry.get("error") or "")
                        print(
                            f"  {'✅ 重试后已确认生效' if two_factor_activated else '⚠️ 重试后取得密钥（未确认激活）'}"
                            f": {two_factor_secret}"
                        )
                    else:
                        print(f"  ⚠️ 重试仍未取到密钥: {res_retry.get('error')}（不写入任何伪造密钥）")
                _report("enable_2fa")
            except Exception as e:  # noqa: BLE001
                two_factor_note = f"exception: {e}"
                print(f"  ⚠️ 自动绑定 2FA 异常: {e}")

        # 12. Codex OAuth Token（可选）
        safe_token = str(token or "")
        tokens = {}
        oauth_status = "未启用"
        if cfg.oauth.enabled:
            print("🔑 开始获取 Codex OAuth Token...")
            try:
                tokens = perform_codex_oauth_login(
                    email=email,
                    password=password,
                    email_provider=email_provider,
                    mail_token=safe_token,
                    proxy=proxy,
                )
                save_codex_tokens(email=email, tokens=tokens, proxy=proxy)
                oauth_status = "成功"
                print("✅ Codex OAuth Token 已保存")
            except Exception as e:  # noqa: BLE001
                oauth_status = f"失败: {e}"
                print(f"❌ OAuth 获取失败: {e}")
                if cfg.oauth.required:
                    save_to_txt(
                        email,
                        password,
                        "已注册/OAuth失败",
                        mailtm_password=str(temp_credential or ""),
                        provider=email_provider,
                        two_factor_secret=two_factor_secret,
                    )
                    return email, password, False

        account_status = "已注册/OAuth成功" if oauth_status == "成功" else "已注册"

        # 13.5 免密账号（邮箱验证码注册分支）：在当前新鲜会话里通过官方设置页补设真实密码
        if not password:
            try:
                from .account_perfector import set_password_via_settings
                from .utils import generate_random_password as _gen_pwd

                candidate = _gen_pwd()
                print("🔑 账号为免密注册，正在通过官方安全设置补设真实密码（需邮箱验证码）...")
                if set_password_via_settings(
                    driver, candidate, print,
                    provider=email_provider, inbox_url=str(temp_credential or ""),
                ):
                    password = candidate
                    print(f"  ✅ 官方确认密码已设置: {password}")
                else:
                    print("  ⚠️ 未确认密码设置成功，密码列保持 N/A（不编造）")
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️ 补设密码异常（忽略）: {exc}")

        # 14. 在线采集真实权益状态（计划 / Plus / 1 个月试用资格 / 到期时间）
        print("🔍 正在通过官方接口检测真实订阅状态与 1 个月 Plus 试用资格...")
        state = collect_real_state(driver, email, web_session, proxy=proxy, log=print)
        print(
            f"  💳 计划: {state['plan']} | Plus: {state.get('is_plus')} | "
            f"试用资格: {state['trial_status']} | 到期: {state['expires_at']}"
        )

        # 14. 保存真实账号画像（含真实 2FA 密钥与证据）
        evidence = {
            "register_mode": "browser",
            "referral_url_configured": bool(target_ref),
            "referral_binding_verified": False,
            "verified": bool(state.get("verified")),
            "is_paid": state.get("is_paid"),
            "is_plus": state.get("is_plus"),
            "trial_eligible": state.get("trial_eligible"),
            "checked_at": state.get("checked_at"),
            "sources": state.get("sources", []),
            "plan_raw": state.get("plan_raw", ""),
            "plan_source": state.get("plan_source", ""),
            "probe_evidence": state.get("evidence", {}),
            "web_token_saved": bool(web_session.get("access_token")),
            "two_factor": {
                "bound": bool(two_factor_secret),
                "activated": bool(two_factor_activated),
                "note": two_factor_note,
            },
        }

        save_to_txt(
            email,
            password,
            account_status,
            mailtm_password=str(temp_credential or ""),
            provider=email_provider,
            plan=state["plan"],
            trial_status=state["trial_status"],
            quota=state["quota"],
            expires_at=state["expires_at"],
            account_status=state["account_status"],
            two_factor_secret=two_factor_secret,
            evidence=evidence,
        )

        print("\n" + "=" * 50)
        print("🎉 注册成功！")
        print(f"   邮箱: {email}")
        print(f"   密码: {password}")
        print(f"   邮箱服务: {provider_name}")
        if two_factor_secret:
            print(f"   2FA 密钥: {two_factor_secret}")
        if cfg.oauth.enabled:
            print(f"   OAuth: {oauth_status}")
        print(f"   计划/Plus: {state['plan']} / {state.get('is_plus')}")
        print(f"   试用资格: {state['trial_status']}")
        print(f"   在线状态: {state['account_status']}")
        print("=" * 50)

        success = True
        time.sleep(3)
        _report("registered")

    except InterruptedError:
        print("🛑 任务已被用户强制中断")
        if email:
            update_account_status(email, "用户中断", provider=email_provider)
        return email, password, False

    except Exception as e:  # noqa: BLE001
        print(f"❌ 发生错误: {e}")
        if email and password:
            update_account_status(email, f"错误: {str(e)[:50]}", provider=email_provider)

    finally:
        if driver:
            print("🔒 正在关闭浏览器...")
            try:
                driver.quit()
            except Exception as e:  # noqa: BLE001
                print(f"⚠️ 关闭浏览器时忽略异常: {e}")

    return email, password, success


def run_batch(selected_providers=None):
    """批量注册账号（串行 CLI 模式）"""
    if not selected_providers:
        selected_providers = ["mailtm"]

    print("\n" + "=" * 60)
    print(f"🚀 开始批量注册，目标数量: {TOTAL_ACCOUNTS}")
    print(f"   邮箱服务: {', '.join(selected_providers)}")
    print("=" * 60 + "\n")

    success_count = 0
    fail_count = 0
    registered_accounts = []

    for i in range(TOTAL_ACCOUNTS):
        print("\n" + "#" * 60)
        print(f"📝 正在注册第 {i + 1}/{TOTAL_ACCOUNTS} 个账号")
        print("#" * 60 + "\n")

        provider = random.choice(selected_providers)
        email, password, success = register_one_account(email_provider=provider)

        if success:
            success_count += 1
            registered_accounts.append((email, password))
        else:
            fail_count += 1

        print("\n" + "-" * 40)
        print(f"📊 当前进度: {i + 1}/{TOTAL_ACCOUNTS}")
        print(f"   ✅ 成功: {success_count}")
        print(f"   ❌ 失败: {fail_count}")
        print("-" * 40)

        if i < TOTAL_ACCOUNTS - 1:
            wait_time = random.randint(BATCH_INTERVAL_MIN, BATCH_INTERVAL_MAX)
            print(f"\n⏳ 等待 {wait_time} 秒后继续下一个注册...")
            time.sleep(wait_time)

    print("\n" + "=" * 60)
    print("🏁 批量注册完成")
    print("=" * 60)
    print(f"   总计: {TOTAL_ACCOUNTS}")
    print(f"   ✅ 成功: {success_count}")
    print(f"   ❌ 失败: {fail_count}")

    if registered_accounts:
        print("\n📋 成功注册的账号:")
        for email, password in registered_accounts:
            print(f"   - {email}")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="ChatGPT 浏览器自动化批量注册")
    parser.add_argument("--count", type=int, default=None, help="本轮注册数量")
    parser.add_argument("--auto", action="store_true", help="启用全自动流水线（注册→补全→检测→出报表）")
    parser.add_argument("--stagger", type=int, default=0,
                        help="每个账号之间的注册间隔秒数（放慢节奏可显著降低 IP 被风控概率，建议 60~120）")
    parser.add_argument("--no-completion", action="store_true",
                        help="纯极速模式：跳过浏览器补全（不抓 Web 凭据/不绑 2FA），只做注册+检测")
    args = parser.parse_args()

    if args.count:
        import app.config as config_module

        config_module.TOTAL_ACCOUNTS = args.count
        config_module.cfg.registration.total_accounts = args.count

    if args.auto:
        from .pipeline import run_autopilot

        report = run_autopilot(
            count=args.count or TOTAL_ACCOUNTS,
            providers=["mailtm"],
            parallel=1,
            headless=False,
            proxy=None,
            referral_url=getattr(cfg.registration, "referral_url", ""),
            enable_2fa=getattr(cfg.registration, "enable_2fa", True),
            stagger_seconds=args.stagger,
            skip_completion=args.no_completion,
        )
        print(json.dumps(report.get("summary", {}), ensure_ascii=False, indent=2))
        return

    run_batch()


if __name__ == "__main__":
    main()
