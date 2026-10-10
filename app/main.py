"""
ChatGPT 极速协议批量注册 - 命令行入口 (CLI)
用法:
    python -m app.main
    python -m app.main --count 5 --referral-url https://chatgpt.com/invite/xxxxxx
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from typing import List

from .config import cfg
from .protocol_register import register_one_account
from . import email_providers


def run_batch(
    total_accounts: int | None = None,
    referral_url: str | None = None,
    enable_2fa: bool | None = None,
    selected_providers: List[str] | None = None,
):
    total = total_accounts or getattr(cfg.registration, "total_accounts", 1)
    ref_url = referral_url if referral_url is not None else getattr(cfg.registration, "referral_url", "")
    mfa = enable_2fa if enable_2fa is not None else getattr(cfg.registration, "enable_2fa", True)
    providers = selected_providers or ["mailtm"]

    print("\n" + "=" * 60)
    print(f"🚀 开始极速协议批量注册，目标数量: {total}")
    print(f"   邮箱服务: {', '.join(providers)}")
    if ref_url:
        print(f"   试用邀请: {ref_url}")
    print(f"   自动 2FA: {'开启' if mfa else '关闭'}")
    print("=" * 60 + "\n")

    success_count = 0
    fail_count = 0
    registered_accounts = []

    for i in range(total):
        print("\n" + "#" * 60)
        print(f"📝 正在协议注册第 {i + 1}/{total} 个账号")
        print("#" * 60 + "\n")

        provider = random.choice(providers)
        try:
            email, password, success = register_one_account(
                email_provider=provider,
                referral_url=ref_url,
                enable_2fa=mfa,
            )
            if success:
                success_count += 1
                registered_accounts.append((email, password))
            else:
                fail_count += 1
        except KeyboardInterrupt:
            print("\n🛑 收到中断信号，正在退出批量任务...")
            break
        except Exception as e:
            print(f"❌ 注册异常: {e}")
            fail_count += 1

        print("\n" + "-" * 40)
        print(f"📊 当前进度: {i + 1}/{total} | ✅ 成功: {success_count} | ❌ 失败: {fail_count}")
        print("-" * 40)

        if i < total - 1:
            wait_time = random.randint(getattr(cfg.batch, "interval_min", 5), getattr(cfg.batch, "interval_max", 15))
            print(f"\n⏳ 等待 {wait_time} 秒后继续下一个注册...")
            time.sleep(wait_time)

    print("\n" + "=" * 60)
    print("🏁 极速协议批量注册完成")
    print("=" * 60)
    print(f"   总计: {total} | 成功: {success_count} | 失败: {fail_count}")
    if registered_accounts:
        print("\n📋 成功注册的账号:")
        for em, pw in registered_accounts:
            print(f"   - {em}")
    print("=" * 60)


def run_auto(
    total_accounts: int | None = None,
    referral_url: str | None = None,
    enable_2fa: bool | None = None,
    selected_providers: List[str] | None = None,
    parallel: int = 2,
    verify_all: bool = False,
    stagger_seconds: int = 0,
    skip_completion: bool = False,
):
    """全自动流水线：协议注册 → 浏览器真实补全 → 官方接口检测 → 最终报表。

    ``stagger_seconds`` 注册间隔（防风控）；``skip_completion`` 纯极速模式（跳过浏览器补全）。
    """
    from .pipeline import run_autopilot

    total = total_accounts or getattr(cfg.registration, "total_accounts", 1)
    ref_url = referral_url if referral_url is not None else getattr(cfg.registration, "referral_url", "")
    mfa = enable_2fa if enable_2fa is not None else getattr(cfg.registration, "enable_2fa", True)
    providers = selected_providers or ["mailtm"]

    report = run_autopilot(
        count=total,
        providers=providers,
        parallel=parallel,
        headless=True,
        proxy=None,
        referral_url=ref_url,
        enable_2fa=mfa,
        mode="protocol",
        verify_all=verify_all,
        stagger_seconds=stagger_seconds,
        skip_completion=skip_completion,
    )
    print("\n" + "=" * 60)
    print("📊 最终报表摘要")
    for key, value in (report.get("summary") or {}).items():
        print(f"   {key}: {value}")
    print("=" * 60)
    return report


def main():
    parser = argparse.ArgumentParser(description="ChatGPT 极速协议批量注册 CLI")
    parser.add_argument("-c", "--count", type=int, default=None, help="注册账号数量")
    parser.add_argument("-r", "--referral-url", type=str, default=None, help="试用邀请链接 (Referral URL)")
    parser.add_argument("--no-2fa", action="store_true", help="禁用自动 2FA")
    parser.add_argument("-p", "--provider", type=str, nargs="+", default=None, help="邮箱服务商 (mailtm, gptmail 等)")
    parser.add_argument("--auto", action="store_true", help="全自动流水线（注册→补全→检测→出报表）")
    parser.add_argument("--parallel", type=int, default=2, help="并发数")
    parser.add_argument("--verify-all", action="store_true", help="连带历史账号一起在线复核")
    parser.add_argument("--stagger", type=int, default=0,
                        help="每个账号之间的注册间隔秒数（放慢节奏可显著降低 IP 被风控概率，建议 60~120）")
    parser.add_argument("--no-completion", action="store_true",
                        help="纯极速模式：跳过浏览器补全（不抓 Web 凭据/不绑 2FA），只做注册+检测")

    args = parser.parse_args()
    if args.auto:
        run_auto(
            total_accounts=args.count,
            referral_url=args.referral_url,
            enable_2fa=False if args.no_2fa else None,
            selected_providers=args.provider,
            parallel=args.parallel,
            verify_all=args.verify_all,
            stagger_seconds=args.stagger,
            skip_completion=args.no_completion,
        )
        return
    run_batch(
        total_accounts=args.count,
        referral_url=args.referral_url,
        enable_2fa=False if args.no_2fa else None,
        selected_providers=args.provider,
    )


if __name__ == "__main__":
    main()
