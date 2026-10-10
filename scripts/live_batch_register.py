"""批量真机注册：把一组真实邮箱逐个跑完整流程，并汇总每个账号真实结果。

每个账号会依次完成（全部真实执行，无模拟）:
  真实创建邮箱会话 → 真实提交注册 → 真实读取收件箱验证码 → 真实资料页提交
  → 抓真实 Web 凭据 → 官方确认的 2FA(TOTP 32位密钥) → 官方接口采集权益

用法::

    python scripts/live_batch_register.py --accounts-file accounts.txt --proxy 127.0.0.1:7897

账号文件每行::

    email@icloud.com----https://icloud-api.top/s/xxx/email@icloud.com
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gpt-auto-register"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def parse_accounts(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip().rstrip(";")
        if not line or line.startswith("#"):
            continue
        if "----" in line:
            email, _, inbox = line.partition("----")
        elif "|" in line:
            email, _, inbox = line.partition("|")
        else:
            email, inbox = line, ""
        email = email.strip()
        inbox = inbox.strip() or f"https://icloud-api.top/s/faHDMh9uBG1VRXzV0Hfgdo5GvNEzmGJy/{email}"
        if "@" in email:
            pairs.append((email, inbox))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description="批量真机注册 + 真实结果汇总")
    parser.add_argument("--accounts-file", required=True)
    parser.add_argument("--provider", default="icloud")
    parser.add_argument("--proxy", default="127.0.0.1:7897")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个（0=全部）")
    parser.add_argument("--out", default=str(ROOT / "token_exports" / "batch_result.json"))
    parser.add_argument("--stagger", type=int, default=0,
                        help="每个账号之间的注册间隔秒数（防风控，建议 60~120；0=连续跑）")
    args = parser.parse_args()

    from app.config import cfg, PROJECT_ROOT
    from app.icloud_service import configure_icloud_account
    from app.stored_accounts import get_registered_account_by_email
    from app.main import register_one_account

    accounts = parse_accounts(Path(args.accounts_file).read_text(encoding="utf-8"))
    if args.limit:
        accounts = accounts[: args.limit]
    if not accounts:
        print("❌ 没有解析到账号")
        return 1

    proxy = None
    if args.proxy and args.proxy.lower() not in ("off", "none"):
        host, _, port = args.proxy.partition(":")
        proxy = {"enabled": True, "type": "http", "host": host, "port": int(port or 7897),
                 "use_auth": False}

    accounts_file = Path(cfg.files.accounts_file)
    if not accounts_file.is_absolute():
        accounts_file = PROJECT_ROOT / accounts_file

    print("=" * 78)
    print(f"🚀 批量真机注册 | 账号数: {len(accounts)} | 渠道: {args.provider} | 代理: {args.proxy}")
    print("=" * 78)

    results: list[dict] = []
    started_all = time.time()

    for index, (email, inbox) in enumerate(accounts, 1):
        print("\n" + "#" * 78)
        print(f"# [{index}/{len(accounts)}] {email}")
        print("#" * 78)
        entry = {"index": index, "email": email, "inbox": inbox, "started_at": datetime.now().isoformat(timespec="seconds")}
        started = time.time()
        try:
            if args.provider == "icloud":
                configure_icloud_account(email, inbox)
            result = register_one_account(
                email_provider=args.provider,
                headless=args.headless,
                proxy=proxy,
                referral_url="",
                enable_2fa=True,
            )
            email_out, password, success = (list(result) + [None, None, False])[:3]
            entry["register_success"] = bool(success)
            entry["skipped"] = bool(getattr(result, "skipped", False))
            entry["password"] = password
        except Exception as exc:  # noqa: BLE001
            entry["register_success"] = False
            entry["error"] = str(exc)[:300]
            print(f"❌ 注册异常: {exc}")

        entry["elapsed_seconds"] = int(time.time() - started)

        record = get_registered_account_by_email(str(accounts_file), email) or {}
        entry.update({
            "status": record.get("status"),
            "plan": record.get("plan"),
            "is_plus": record.get("is_plus"),
            "trial_status": record.get("trial_status"),
            "trial_eligible": record.get("trial_eligible"),
            "expires_at": record.get("expires_at"),
            "account_status": record.get("account_status"),
            "two_factor_secret": record.get("two_factor_secret") or "",
            "two_factor_activated": bool((record.get("profile_evidence") or {}).get("two_factor", {}).get("activated")),
            "verified": bool(record.get("verified")),
            "used": bool(record.get("status") and record.get("status") != "邮箱已创建"),
        })
        entry["finished_at"] = datetime.now().isoformat(timespec="seconds")
        results.append(entry)
        print(
            f"\n📌 [{index}/{len(accounts)}] 汇总: 注册={'成功' if entry['register_success'] else '失败'}"
            f" | 计划={entry['plan']} | Plus={entry['is_plus']} | 试用={entry['trial_status']}"
            f" | 2FA={(entry['two_factor_secret'] or '无')}"
            f"{'（已激活）' if entry['two_factor_activated'] else '（未确认激活）' if entry['two_factor_secret'] else ''}"
        )
        # 节奏控制：放慢注册可显著降低同一个 IP 被风控的概率
        if args.stagger > 0 and index < len(accounts):
            print(f"\n   ⏳ 节奏控制：等待 {args.stagger}s 后再注册下一个 ...")
            for _ in range(args.stagger):
                time.sleep(1)

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(results),
        "used": sum(1 for r in results if r.get("used")),
        "register_ok": sum(1 for r in results if r.get("register_success")),
        "with_2fa_secret": sum(1 for r in results if r.get("two_factor_secret")),
        "with_2fa_activated": sum(1 for r in results if r.get("two_factor_activated")),
        "plus_active": sum(1 for r in results if r.get("is_plus")),
        "trial_eligible": sum(1 for r in results if r.get("trial_eligible")),
        "total_seconds": int(time.time() - started_all),
        "accounts": results,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 120)
    print(f"📊 批量结果汇总 | 使用: {summary['used']}/{summary['total']} | 注册成功: {summary['register_ok']}"
          f" | 含32位2FA: {summary['with_2fa_secret']}（已激活 {summary['with_2fa_activated']}）"
          f" | Plus: {summary['plus_active']} | 有试用资格: {summary['trial_eligible']}"
          f" | 用时 {summary['total_seconds']}s")
    print("=" * 120)
    header = f"{'邮箱':<32}{'使用':<6}{'注册':<6}{'密码':<20}{'32位2FA密钥':<36}{'激活':<6}{'计划':<8}{'试用资格':<28}"
    print(header)
    print("-" * len(header))
    for row in results:
        print(
            f"{(row.get('email') or '')[:31]:<32}"
            f"{'✅' if row.get('used') else '❌':<6}"
            f"{'✅' if row.get('register_success') else '❌':<6}"
            f"{str(row.get('password') or 'N/A')[:19]:<20}"
            f"{(row.get('two_factor_secret') or '-'):<36}"
            f"{'✅' if row.get('two_factor_activated') else ('未确认' if row.get('two_factor_secret') else '-'):<6}"
            f"{str(row.get('plan')):<8}"
            f"{str(row.get('trial_status'))[:26]:<28}"
        )
    print(f"\n💾 明细已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
