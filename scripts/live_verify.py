"""真机验证工具：在本地对真实账号跑一遍官方接口，打印真实结果与证据。

用法（在仓库根目录执行）::

    # 1) 用本地已保存的 Web access token 直连官方接口，看真实计划/Plus/试用资格/到期时间
    python scripts/live_verify.py --email you@example.com

    # 2) 真机浏览器登录（支持密码或邮箱验证码分支）→ 抓真实凭据 → 官方确认后绑定真实 2FA
    python scripts/live_verify.py --email you@example.com --browser

    # 3) 一次性打印全部账号的真实报表（不出网，只读本地已保存证据）
    python scripts/live_verify.py --report

    # 4) 强制走协议版代码路径
    python scripts/live_verify.py --email you@example.com --edition protocol

注意：本工具只使用官方接口真实返回的数据。任何拿不到证据的字段都会显示"未知"，
不会用本地配置、邀请链接或注册成功来推断试用资格。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDITIONS = {
    "browser": ROOT / "gpt-auto-register",
    "protocol": ROOT / "gpt-protocol-register",
}

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _bootstrap(edition: str):
    project = EDITIONS[edition]
    if not project.is_dir():
        raise SystemExit(f"❌ 找不到项目目录: {project}")
    sys.path.insert(0, str(project))
    return project


def main() -> int:
    parser = argparse.ArgumentParser(description="ChatGPT 账号真机验证工具")
    parser.add_argument("--email", help="要验证的账号邮箱")
    parser.add_argument("--edition", choices=sorted(EDITIONS), default="browser", help="使用哪套代码路径")
    parser.add_argument("--browser", action="store_true", help="启动真实浏览器登录并补全真实 2FA（较慢）")
    parser.add_argument("--report", action="store_true", help="打印当前所有账号的真实报表")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出完整结果（含原始报文证据）")
    parser.add_argument("--headless", action="store_true", help="浏览器模式使用无界面运行")
    args = parser.parse_args()

    project = _bootstrap(args.edition)

    if args.report or not args.email:
        from app.pipeline import build_report_now

        report = build_report_now()
        rows = report.get("rows", [])
        print(f"📊 账号报表（{len(rows)} 条，来源: {report.get('accounts_file')}）\n")
        header = (
            f"{'邮箱':<34} {'密码':<12} {'2FA':<12} {'动态码':<8} "
            f"{'试用资格':<28}{'Plus':<8}{'到期时间':<22}"
        )
        print(header)
        print("-" * len(header))
        for row in rows:
            secret = row.get("two_factor_secret") or ""
            password = str(row.get("password") or "N/A")
            if len(password) > 12:
                password = password[:11] + "…"
            print(
                f"{row.get('email', ''):<34}"
                f"{password:<12} "
                f"{(secret[:8] + '…' if len(secret) > 8 else secret or '未开启'):<12} "
                f"{(row.get('two_factor_now') or '-'):<8} "
                f"{str(row.get('trial_status'))[:26]:<28}"
                f"{str(row.get('is_plus')):<8}"
                f"{str(row.get('expires_at'))[:20]:<22}"
            )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.browser:
        from app.account_perfector import perfect_single_account

        print(f"🌐 真机浏览器验证: {args.email}（含真实 2FA 绑定与官方权益采集）")
        result = perfect_single_account(email=args.email, headless=args.headless)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("success") else 1

    from app.account_checker import probe_account_state

    print(f"🔍 直连官方接口验证: {args.email}（优先本地已保存的 Web access token）")
    state = probe_account_state(args.email)
    print(json.dumps(state if args.json else {
        "email": state.get("email"),
        "plan": state.get("plan"),
        "plan_raw": state.get("plan_raw"),
        "is_plus": state.get("is_plus"),
        "is_paid": state.get("is_paid"),
        "trial_status": state.get("trial_status"),
        "trial_eligible": state.get("trial_eligible"),
        "quota": state.get("quota"),
        "expires_at": state.get("expires_at"),
        "account_status": state.get("account_status"),
        "verified": state.get("verified"),
        "checked_at": state.get("checked_at"),
        "sources": state.get("sources"),
    }, ensure_ascii=False, indent=2, default=str))
    if not state.get("verified"):
        print("\n⚠️ 未取得官方证据（凭证缺失/过期或被 Cloudflare 拦截）。")
        print("   → 建议加 --browser 走真实浏览器会话，或先在面板点【完善】重新抓取凭据。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
