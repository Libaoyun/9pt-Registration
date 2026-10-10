"""
批量从已有 TXT 账号记录中补取 Codex token。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from .config import cfg
from .oauth_service import perform_codex_oauth_login, save_codex_tokens
from .stored_accounts import (
    OAUTH_SUCCESS_STATUS,
    is_oauth_success_status,
    load_accounts_from_file,
    update_account_status_in_file,
)


def _build_output_oauth_cfg(output_dir: str):
    os.makedirs(output_dir, exist_ok=True)
    return SimpleNamespace(
        ak_file=cfg.oauth.ak_file,
        rk_file=cfg.oauth.rk_file,
        token_json_dir=output_dir,
    )


def _login_existing_mailbox(provider: str, email: str, mailbox_credential: str):
    from . import email_providers

    provider_info = email_providers.get_provider_info(provider)
    if not provider_info:
        raise RuntimeError(f"未知邮箱 provider: {provider}")

    login_func = getattr(provider_info["module"], "login_existing_email", None)
    if not callable(login_func):
        raise RuntimeError(f"provider={provider} 暂不支持重新登录收件箱")

    return login_func(email, mailbox_credential)


def process_accounts_from_file(
    accounts_file: str,
    output_dir: str,
    proxy: dict | None = None,
    stop_requested=None,
    progress_callback=None,
    mail_login_func=_login_existing_mailbox,
    oauth_login_func=perform_codex_oauth_login,
    save_tokens_func=save_codex_tokens,
):
    records = load_accounts_from_file(accounts_file)
    oauth_cfg = _build_output_oauth_cfg(output_dir)
    success = 0
    fail = 0
    processed = 0
    skipped = 0
    total = len(records)

    def report_progress(current_email: str = "", status: str = ""):
        completed = success + fail + skipped
        if progress_callback:
            progress_callback(
                {
                    "task_type": "token_import",
                    "total": total,
                    "processed": processed,
                    "completed": completed,
                    "success": success,
                    "fail": fail,
                    "skipped": skipped,
                    "remaining": max(total - completed, 0),
                    "current_email": current_email,
                    "status": status,
                }
            )

    report_progress(status="starting")

    for record in records:
        if stop_requested and stop_requested():
            break

        email = record["email"]
        provider = record["provider"].strip().lower()
        print(f"📄 处理账号: {email} ({provider})")

        if is_oauth_success_status(record["status"]):
            skipped += 1
            print(f"⏭️ 跳过 {email}: 已是 OAuth 成功状态")
            report_progress(current_email=email, status="skipped_existing_success")
            continue

        mailbox_credential = record["mailbox_credential"]
        if not mailbox_credential:
            fail += 1
            print(f"⚠️ 跳过 {email}: 缺少邮箱收件凭证")
            report_progress(current_email=email, status="missing_mailbox_credential")
            continue

        try:
            mail_token = mail_login_func(provider, email, mailbox_credential)
            tokens = oauth_login_func(
                email=email,
                password=record["password"],
                email_provider=provider,
                mail_token=mail_token,
                proxy=proxy,
            )
            save_tokens_func(
                email=email,
                tokens=tokens,
                oauth_cfg=oauth_cfg,
                proxy=proxy,
            )
            plan_status = "未检测"
            trial_status = "待官方确认"
            quota_status = "未知，请在官方页面确认"
            expires_status = "未知，未取得订阅到期时间"
            health_status = "⚪ 未在线验证"
            try:
                from .account_checker import check_account_status
                chk = check_account_status(email, tokens.get("access_token"), proxy=proxy, token_kind="codex")
                plan_status = chk.get("plan", "未检测")
                trial_status = chk.get("trial_status", "待官方确认")
                quota_status = chk.get("quota", "未知，请在官方页面确认")
                expires_status = chk.get("expires_at", "未知，未取得订阅到期时间")
                health_status = chk.get("account_status", "⚪ 未在线验证")
            except Exception:
                pass
            update_account_status_in_file(
                accounts_file,
                email,
                OAUTH_SUCCESS_STATUS,
                plan=plan_status,
                trial_status=trial_status,
                quota=quota_status,
                expires_at=expires_status,
                account_status=health_status,
            )
            success += 1
            processed += 1
            print(f"✅ 已生成 Token: {email} | 计划: {plan_status} | 试用: {trial_status} | 额度: {quota_status}")
            report_progress(current_email=email, status="success")
        except Exception as exc:
            fail += 1
            print(f"❌ 处理失败 {email}: {exc}")
            report_progress(current_email=email, status="failed")

    completed = success + fail + skipped

    return {
        "total": total,
        "processed": processed,
        "completed": completed,
        "success": success,
        "fail": fail,
        "skipped": skipped,
        "remaining": max(total - completed, 0),
        "output_dir": output_dir,
    }
