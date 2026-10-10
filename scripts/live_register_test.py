"""真机单账号注册测试：用真实邮箱真跑一遍注册，并打印每一步的真实结果。

它只做真事，不做模拟：真实启动浏览器 / 真实发包、真实读收件箱、真实提交验证码，
注册完成后立刻抓真实 Web 凭据、真实开通 2FA、真实查询官方权益。

用法（仓库根目录）::

    # 浏览器版 + iCloud 邮箱 + 本机代理
    python scripts/live_register_test.py --edition browser --provider icloud ^
        --email you@icloud.com --inbox "https://icloud-api.top/s/xxx/you@icloud.com" ^
        --proxy 127.0.0.1:7897

    # 协议版（注册后自动用浏览器通道补全真实 2FA 与权益证据）
    python scripts/live_register_test.py --edition protocol --provider icloud ... --auto

    # 只读回本地最新记录，不发请求
    python scripts/live_register_test.py --email you@icloud.com --read-only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDITIONS = {
    "browser": ROOT / "gpt-auto-register",
    "protocol": ROOT / "gpt-protocol-register",
}
PROXY_SCHEMES = ("http", "socks5")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _parse_proxy(raw: str | None) -> dict | None:
    if not raw or raw.lower() in ("none", "off", "0"):
        return None
    scheme = "http"
    value = raw
    if "://" in value:
        scheme, value = value.split("://", 1)
        scheme = scheme if scheme in PROXY_SCHEMES else "http"
    host, _, port = value.partition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"❌ 代理格式应为 host:port 或 scheme://host:port，收到: {raw}")
    return {
        "enabled": True, "type": scheme, "host": host, "port": int(port),
        "use_auth": False, "username": "", "password": "",
    }


def _bootstrap(edition: str) -> Path:
    project = EDITIONS[edition]
    if not project.is_dir():
        raise SystemExit(f"❌ 找不到项目目录: {project}")
    sys.path.insert(0, str(project))
    return project


def _print_record(email: str) -> dict | None:
    from app.stored_accounts import get_registered_account_by_email
    from app.config import cfg, PROJECT_ROOT

    path = Path(cfg.files.accounts_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    record = get_registered_account_by_email(str(path), email)
    if not record:
        print(f"⚠️ 本地账号库中没有 {email}")
        return None
    print("\n" + "=" * 72)
    print("📄 本地账号库真实记录")
    print("=" * 72)
    for key in ("email", "password", "timestamp", "status", "provider", "plan", "is_plus",
                "is_paid", "trial_status", "trial_eligible", "quota", "expires_at",
                "account_status", "two_factor_secret", "verified", "register_mode"):
        print(f"  {key:<18}: {record.get(key)}")
    evidence = record.get("profile_evidence") or {}
    if evidence:
        print(f"  {'证据文件':<18}: {evidence.get('evidence_file') or '-'}")
        print(f"  {'接口尝试':<18}: {json.dumps(evidence.get('sources'), ensure_ascii=False)}")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="真机单账号注册测试")
    parser.add_argument("--edition", choices=sorted(EDITIONS), default="browser")
    parser.add_argument("--provider", default="icloud", help="邮箱服务商 (icloud / mailtm / gptmail / tempmail_lol ...)")
    parser.add_argument("--email", help="用于测试的邮箱（icloud / custom2925 需要）")
    parser.add_argument("--inbox", help="收件箱读取链接（icloud 需要）")
    parser.add_argument("--proxy", default="127.0.0.1:7897", help="注册浏览器使用的代理，off 表示直连")
    parser.add_argument("--referral-url", default="", help="官方邀请链接（可选，不构成试用资格证据）")
    parser.add_argument("--headless", action="store_true", help="无界面运行（2FA 的 UI 兜底路径成功率略低）")
    parser.add_argument("--no-2fa", action="store_true", help="本次不自动绑定 2FA")
    parser.add_argument("--auto", action="store_true", help="注册成功后自动跑补全+官方检测（推荐）")
    parser.add_argument("--parallel", type=int, default=2, help="--auto 补全阶段的并发数")
    parser.add_argument("--read-only", action="store_true", help="只读回本地记录，不发任何请求")
    parser.add_argument("--perfect-only", action="store_true",
                        help="跳过注册，直接对已有账号跑【完善】流程（恢复会话/真实2FA/官方权益）")
    parser.add_argument("--set-password", action="store_true",
                        help="用官方「忘记密码」流程给账号设置一个真实密码（需要 --inbox）")
    args = parser.parse_args()

    _bootstrap(args.edition)
    proxy = _parse_proxy(args.proxy)

    if args.set_password:
        if not (args.email and args.inbox):
            raise SystemExit("❌ --set-password 需要 --email 与 --inbox")
        from app.account_perfector import set_initial_password
        from app.browser import create_driver
        from app.icloud_service import configure_icloud_account

        if args.provider == "icloud":
            configure_icloud_account(args.email, args.inbox)
        driver = create_driver(headless=args.headless, proxy=proxy)
        try:
            driver.get("https://chatgpt.com")
            time.sleep(3)
            new_password = set_initial_password(
                driver, args.email, args.provider, args.inbox, set(), log_func=print,
            )
            print(f"\n🏁 设置密码结果: {new_password or '未成功'}")
            if new_password:
                from app.utils import save_to_txt
                save_to_txt(args.email, new_password, "已注册", mailtm_password=args.inbox,
                            provider=args.provider)
                print(f"   💾 已写回账号库: {args.email} / {new_password}")
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        return 0 if new_password else 1

    if args.perfect_only:
        if not args.email:
            raise SystemExit("❌ --perfect-only 需要 --email")
        from app.account_perfector import perfect_single_account

        print(f"⚡ 仅跑完善流程: {args.email} | 代理: {args.proxy if proxy else '直连'}")
        result = perfect_single_account(
            email=args.email, proxy=proxy, headless=args.headless, log_func=print,
        )
        print(json.dumps({k: v for k, v in result.items() if k != "state"},
                         ensure_ascii=False, indent=2, default=str))
        _print_record(args.email)
        return 0 if result.get("success") else 1

    if args.read_only:
        if not args.email:
            raise SystemExit("❌ --read-only 需要 --email")
        _print_record(args.email)
        return 0

    if args.provider == "icloud":
        if not args.email or not args.inbox:
            raise SystemExit("❌ icloud 渠道需要同时提供 --email 与 --inbox")
        from app.icloud_service import configure_icloud_account

        configure_icloud_account(args.email, args.inbox)
        print(f"📧 iCloud 渠道已配置: {args.email}")
    elif args.email:
        print(f"ℹ️ provider={args.provider} 会自行创建临时邮箱，--email 仅用于回读记录")

    started = time.time()
    print(f"🚀 开始真机注册测试 | 版本: {args.edition} | 渠道: {args.provider} | "
          f"代理: {args.proxy if proxy else '直连'} | 2FA: {'关闭' if args.no_2fa else '开启'}")
    print("-" * 72)

    try:
        if args.edition == "protocol":
            from app.protocol_register import register_one_account
        else:
            from app.main import register_one_account

        result = register_one_account(
            email_provider=args.provider,
            headless=args.headless,
            proxy=proxy,
            referral_url=args.referral_url,
            enable_2fa=not args.no_2fa,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n💥 注册流程抛出异常: {exc}")
        return 1

    email, password, success = (list(result) + [None, None, False])[:3]
    skipped = bool(getattr(result, "skipped", False))
    elapsed = int(time.time() - started)

    print("\n" + "=" * 72)
    print(f"🏁 注册结果: {'✅ 成功' if success else '❌ 失败'}"
          f"{'（已存在，跳过注册）' if skipped else ''} | 耗时 {elapsed}s")
    print(f"   邮箱: {email}")
    print(f"   密码: {password}")
    print("=" * 72)

    if email:
        _print_record(email)

    if args.auto and email and success:
        print("\n" + "=" * 72)
        print("🤖 自动补全 + 官方接口检测（真实 2FA / 真实权益证据）")
        print("=" * 72)
        from app.account_perfector import perfect_single_account

        perfect = perfect_single_account(
            email=email,
            password=password,
            provider=args.provider,
            proxy=proxy,
            headless=True,
            log_func=print,
        )
        print(json.dumps({k: v for k, v in perfect.items() if k != "state"},
                         ensure_ascii=False, indent=2, default=str))
        _print_record(email)

        from app.pipeline import build_report_now

        report = build_report_now()
        for row in report.get("rows", []):
            if row.get("email") == email:
                print("\n📊 最终报表行（真实字段）:")
                print(json.dumps(row, ensure_ascii=False, indent=2, default=str))

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
