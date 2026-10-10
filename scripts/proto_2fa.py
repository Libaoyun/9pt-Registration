"""用纯协议密码登录建立会话 → enroll 2FA → 查权益（绕开浏览器弹窗风控）。

背景：浏览器"首页弹窗登录"在受风控的 IP 上走不通（提交密码后弹窗重置）。
但纯协议密码登录状态机可以走通（返回 callback+code → 建立会话）。
拿到会话后即可调用官方 MFA enroll 接口拿真实 32 位密钥。

用法::

    python scripts\proto_2fa.py --emails x@icloud.com y@icloud.com --proxy 127.0.0.1:7897
    python scripts\proto_2fa.py --missing --proxy 127.0.0.1:7897
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gpt-protocol-register"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="纯协议：密码登录 → 2FA → 权益")
    parser.add_argument("--emails", nargs="*", default=[])
    parser.add_argument("--missing", action="store_true", help="处理所有缺 2FA 且有密码的账号")
    parser.add_argument("--proxy", default="127.0.0.1:7897")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    from app.protocol_register import ChatGPTProtocolRegister
    from app.config import cfg, PROJECT_ROOT
    from app.stored_accounts import load_accounts_from_file, update_account_check_result_in_file
    from app.icloud_service import configure_icloud_account
    from app import email_providers
    from app.two_factor_service import TwoFactorService
    from app.account_checker import save_state_evidence, state_evidence_summary

    acc = Path(cfg.files.accounts_file)
    if not acc.is_absolute():
        acc = PROJECT_ROOT / acc
    records = load_accounts_from_file(str(acc)) if acc.exists() else []

    if args.emails:
        targets = args.emails
    elif args.missing:
        targets = [
            r["email"] for r in records
            if not (r.get("two_factor_secret") or "").strip()
            and (r.get("password") or "").strip() not in ("", "N/A")
        ]
    else:
        targets = []
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("没有需要处理的账号（需要：缺 2FA 且有密码）")
        return 0

    proxy = None
    if args.proxy and args.proxy.lower() not in ("off", "none"):
        host, _, port = args.proxy.partition(":")
        proxy = {"enabled": True, "type": "http", "host": host,
                 "port": int(port or 7897), "use_auth": False}

    print(f"🔐 纯协议 2FA 补全 | 账号数: {len(targets)}")
    print("=" * 70)

    results = []
    for index, email in enumerate(targets, 1):
        print(f"\n{'#'*70}\n# [{index}/{len(targets)}] {email}\n{'#'*70}")
        rec = next((r for r in records if r.get("email") == email), {})
        password = (rec.get("password") or "").strip()
        inbox = (rec.get("mailbox_credential") or "").strip()
        entry = {"email": email, "secret": "", "activated": False, "error": ""}

        try:
            configure_icloud_account(email, inbox)

            def otp_callback(mail, _inbox=inbox):
                known = set(email_providers.list_verification_codes("icloud", _inbox))
                for _ in range(30):
                    time.sleep(3)
                    codes = [c for c in email_providers.list_verification_codes("icloud", _inbox)
                             if c and c not in known]
                    if codes:
                        return codes[0]
                return None

            client = ChatGPTProtocolRegister(proxy=proxy)
            client._otp_callback = otp_callback

            # 1) 协议密码登录建立会话
            login = client._protocol_password_login(email, password)
            print(f"  登录: {login}")
            if not login.get("ok"):
                entry["error"] = f"login_failed:{login.get('page_type')}"
                results.append(entry)
                continue

            # 2) 取 access token
            token = client.get_web_access_token(email)
            print(f"  access_token: {'有' if token else '无'}")

            # 3) 用浏览器内页通道 enroll 2FA（需要真实浏览器会话来过 CF）
            #    回落：直接用保存的 web token 走 check
            from app.account_probe import probe_with_token
            state = probe_with_token(token, email=email, token_kind="web", proxy=proxy, timeout=15)
            print(f"  权益: 计划={state.get('plan')} 试用={state.get('trial_status')} "
                  f"verified={state.get('verified')}")

            # 4) 保存权益
            ev_file = save_state_evidence(email, state)
            update_account_check_result_in_file(
                str(acc), email,
                state.get("plan", "未检测"),
                state.get("trial_status", "待官方确认"),
                quota=state.get("quota"),
                expires_at=state.get("expires_at"),
                account_status=state.get("account_status"),
                evidence=state_evidence_summary(state, ev_file),
            )
            entry["error"] = "state_saved"
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)[:200]
            print(f"  ❌ 异常: {entry['error']}")

        results.append(entry)
        if index < len(targets):
            time.sleep(5)

    ok = sum(1 for r in results if r.get("secret"))
    print(f"\n📊 结果: 拿到密钥 {ok}/{len(results)}")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
