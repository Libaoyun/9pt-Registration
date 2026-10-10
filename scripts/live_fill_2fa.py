"""为已注册账号补齐 32 位 2FA 密钥。

原理：用本地保存的真实 cookie 恢复登录态后，直接调用官方
``POST /backend-api/accounts/mfa/enroll``（带 Bearer）取回真实密钥。

注意：官方对该接口有 `recent_auth_required` 限制——会话不新鲜时会返回 401，
此时会如实告知"需要重新登录"，不会伪造任何密钥。

用法::

    python scripts/live_fill_2fa.py --emails a@icloud.com b@icloud.com --proxy 127.0.0.1:7897
    python scripts/live_fill_2fa.py --missing --proxy 127.0.0.1:7897      # 自动挑出缺密钥的账号
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gpt-auto-register"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="为已注册账号补齐真实 32 位 2FA 密钥")
    parser.add_argument("--emails", nargs="*", default=[])
    parser.add_argument("--missing", action="store_true", help="自动处理所有缺密钥的已注册账号")
    parser.add_argument("--proxy", default="127.0.0.1:7897")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    from app.account_perfector import _restore_saved_session, fresh_login_via_email_code
    from app.browser import create_driver
    from app.config import cfg, PROJECT_ROOT
    from app.stored_accounts import (
        load_accounts_from_file, update_account_check_result_in_file,
    )
    from app.two_factor_service import enable_account_2fa

    path = Path(cfg.files.accounts_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    records = load_accounts_from_file(str(path)) if path.exists() else []

    targets = list(args.emails)
    if args.missing:
        for record in records:
            secret = (record.get("two_factor_secret") or "").strip()
            if not secret or secret in ("未开启", "N/A"):
                if record.get("status") and record["status"] != "邮箱已创建":
                    targets.append(record["email"])
    targets = list(dict.fromkeys(targets))
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("✅ 没有需要补齐密钥的账号")
        return 0

    proxy = None
    if args.proxy and args.proxy.lower() not in ("off", "none"):
        host, _, port = args.proxy.partition(":")
        proxy = {"enabled": True, "type": "http", "host": host, "port": int(port or 7897),
                 "use_auth": False}

    results = []
    for index, email in enumerate(targets, 1):
        print("\n" + "#" * 70)
        print(f"# [{index}/{len(targets)}] {email}")
        print("#" * 70)
        entry = {"email": email, "secret": "", "activated": False, "error": ""}
        driver = None
        record = next((r for r in records if r["email"] == email), {})
        provider = record.get("provider") or "icloud"
        inbox_url = (record.get("mailbox_credential") or "").strip()
        try:
            driver = create_driver(headless=args.headless, proxy=proxy)

            # ① 先试用本地 cookie 恢复会话（最快）
            restored = _restore_saved_session(driver, email, print)

            def _try_enroll() -> dict:
                return enable_account_2fa(driver, log_func=print)

            result = _try_enroll() if restored else {"secret": "", "activated": False,
                                                    "error": "cookie_restore_failed"}
            entry["error"] = str(result.get("error") or "")

            # ② 只要没拿到密钥，就做一次真实登录（优先密码方式）再立刻 enroll
            if not result.get("secret"):
                print("  ↪️ 需要新鲜认证，改为真实登录后立即绑定 2FA ...")
                account_password = record.get("password") or ""
                if account_password in ("N/A", ""):
                    account_password = None
                logged_in = fresh_login_via_email_code(
                    driver, email, provider, inbox_url, print,
                    password=account_password,
                )
                if not logged_in:
                    # enroll / UI 兜底可能把浏览器会话折腾挂了：换一个全新浏览器再试一次
                    print("  ↪️ 首次新鲜登录未成功，重建浏览器再试一次 ...")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = create_driver(headless=args.headless, proxy=proxy)
                    logged_in = fresh_login_via_email_code(
                        driver, email, provider, inbox_url, print,
                        password=account_password,
                    )
                if logged_in:
                    result = _try_enroll()
                    entry["error"] = str(result.get("error") or "")
                else:
                    entry["error"] = "fresh_login_failed"

            entry["secret"] = result.get("secret", "") or ""
            entry["activated"] = bool(result.get("activated"))
            entry["error"] = entry["error"] or str(result.get("error") or "")
            if entry["secret"]:
                evidence = dict(record.get("profile_evidence") or {})
                evidence["two_factor"] = {
                    "bound": True,
                    "activated": entry["activated"],
                    "verified_by": result.get("verified_by", ""),
                    "note": result.get("error") or "",
                }
                update_account_check_result_in_file(
                    str(path), email, record.get("plan", "未检测"),
                    record.get("trial_status", "待官方确认"),
                    quota=record.get("quota"), expires_at=record.get("expires_at"),
                    account_status=record.get("account_status"),
                    two_factor_secret=entry["secret"], evidence=evidence,
                )
                print(f"  💾 已写回密钥: {entry['secret']}")
            elif not entry["error"]:
                entry["error"] = "secret_not_returned"
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)[:200]
            print(f"  ❌ 异常: {exc}")
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
        results.append(entry)

    print("\n" + "=" * 96)
    print("📊 补齐结果")
    print("=" * 96)
    for row in results:
        mark = "✅" if row["secret"] else "❌"
        print(f"{mark} {row['email']:<32} {row['secret'] or '-':<36} {row['error'][:40]}")
    ok = sum(1 for r in results if r["secret"])
    print(f"\n成功补齐: {ok}/{len(results)}")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
