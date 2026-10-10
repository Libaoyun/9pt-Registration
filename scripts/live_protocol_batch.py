"""协议版批量真机注册（验证"注册即带密码"）。

与 live_batch_register.py 的区别：走 gpt-protocol-register 的协议引擎，
注册接口直接传密码（POST /api/accounts/user/register {"username","password"}），
因此注册成功即拥有密码 —— 登录三件套 = 邮箱 + 密码 + 2FA。

用法::
    python scripts/live_protocol_batch.py --accounts-file accounts.txt \
        --proxy 127.0.0.1:7897 [--stagger 120] [--limit 1]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gpt-protocol-register"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def parse_accounts(text: str) -> list[tuple[str, str]]:
    pairs = []
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
        email = email.strip().lstrip("\ufeff")
        inbox = inbox.strip() or f"https://icloud-api.top/s/faHDMh9uBG1VRXzV0Hfgdo5GvNEzmGJy/{email}"
        if "@" in email:
            pairs.append((email, inbox))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description="协议版批量真机注册（带密码）")
    parser.add_argument("--accounts-file", required=True)
    parser.add_argument("--provider", default="icloud")
    parser.add_argument("--proxy", default="127.0.0.1:7897")
    parser.add_argument("--stagger", type=int, default=0, help="每个账号之间等待秒数（防风控）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个")
    parser.add_argument("--out", default=str(ROOT / "token_exports" / "protocol_batch_result.json"))
    parser.add_argument("--rotate-node", action="store_true",
                        help="每注册 N 个账号自动切换 Clash 节点（换 IP = 配额重置）")
    parser.add_argument("--rotate-every", type=int, default=1, help="每 N 个账号切换一次节点（默认1）")
    parser.add_argument("--clash-api", default="http://127.0.0.1:9097")
    parser.add_argument("--clash-secret", default="")
    args = parser.parse_args()

    from app.config import cfg, PROJECT_ROOT
    from app.protocol_register import register_one_account
    from app.stored_accounts import load_accounts_from_file
    from app.icloud_service import configure_icloud_account

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
    print(f"⚡ 协议版批量真机注册 | 数量: {len(accounts)} | 渠道: {args.provider} | 代理: {args.proxy}")
    print("   协议注册直接传密码 → 注册成功即带密码")
    print("=" * 78)

    results = []
    started_all = time.time()

    for index, (email, inbox) in enumerate(accounts, 1):
        print("\n" + "#" * 78)
        print(f"# [{index}/{len(accounts)}] {email}")
        print("#" * 78)
        entry = {"index": index, "email": email, "password": "", "register_success": False,
                 "started_at": datetime.now().isoformat(timespec="seconds")}
        started = time.time()
        try:
            if args.provider == "icloud":
                configure_icloud_account(email, inbox)
            result = register_one_account(
                email_provider=args.provider,
                headless=True,
                proxy=proxy,
                referral_url="",
                enable_2fa=True,
            )
            out_email, out_password, success = (list(result) + [None, None, False])[:3]
            entry["register_success"] = bool(success)
            entry["password"] = out_password
            entry["skipped"] = bool(getattr(result, "skipped", False))
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)[:300]
            print(f"❌ 注册异常: {exc}")

        entry["elapsed_seconds"] = int(time.time() - started)

        records = load_accounts_from_file(str(accounts_file)) if accounts_file.exists() else []
        record = next((r for r in records if r["email"] == email), {})
        secret = (record.get("two_factor_secret") or "").strip()
        has_secret = bool(secret) and secret not in ("未开启", "N/A")
        entry.update({
            "status": record.get("status"),
            "plan": record.get("plan"),
            "is_plus": record.get("is_plus"),
            "trial_status": record.get("trial_status"),
            "two_factor_secret": secret,
            "verified": bool(record.get("verified")),
            "has_password": bool(entry["password"]) and entry["password"] not in ("N/A", ""),
        })
        entry["finished_at"] = datetime.now().isoformat(timespec="seconds")
        results.append(entry)

        print(f"\n📌 [{index}/{len(accounts)}] 汇总: 注册={'成功' if entry['register_success'] else '失败'}"
              f" | 密码={'✅ ' + str(entry['password']) if entry['has_password'] else '无'}"
              f" | 计划={entry['plan']} | 2FA={(secret or '无')[:12] + '…' if has_secret else '无'}")

        if args.stagger > 0 and index < len(accounts):
            print(f"\n   ⏳ 节奏控制：等待 {args.stagger}s ...")
            for _ in range(args.stagger):
                time.sleep(1)

        # 节点轮换：换 IP = 配额重置（Clash 外部控制 API）
        if args.rotate_node and index < len(accounts) and index % max(1, args.rotate_every) == 0:
            try:
                sys.path.insert(0, str(ROOT / "scripts"))
                from clash_rotate import rotate
                new_node = rotate(args.clash_api, args.clash_secret)
                print(f"\n🔄 [节点轮换] 已切换到: {new_node}（等 5s 让连接稳定）")
                time.sleep(5)
            except Exception as exc:  # noqa: BLE001
                print(f"\n⚠️ 节点轮换失败（继续用当前节点）: {exc}")

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "engine": "protocol",
        "total": len(results),
        "register_ok": sum(1 for r in results if r.get("register_success")),
        "with_password": sum(1 for r in results if r.get("has_password")),
        "with_2fa_secret": sum(1 for r in results if r.get("two_factor_secret")),
        "total_seconds": int(time.time() - started_all),
        "accounts": results,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 96)
    print(f"📊 协议版结果 | 注册成功 {summary['register_ok']}/{summary['total']}"
          f" | 带密码 {summary['with_password']} | 带2FA密钥 {summary['with_2fa_secret']}"
          f" | 用时 {summary['total_seconds']}s")
    print("=" * 96)
    print(f"{'邮箱':<32}{'注册':<6}{'密码':<20}{'32位2FA':<34}")
    print("-" * 96)
    for row in results:
        print(f"{row['email'][:31]:<32}{'✅' if row['register_success'] else '❌':<6}"
              f"{str(row.get('password') or '—')[:19]:<20}"
              f"{(row.get('two_factor_secret') or '—'):<34}")
    print(f"\n💾 明细: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
