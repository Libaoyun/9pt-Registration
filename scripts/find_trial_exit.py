"""通过 Clash 节点轮换查找 Plus 试用资格的"好出口"。

原理（焚决·第八条）：同一个账号，不同的查询出口 IP 会得到不同的
`eligible_promo_campaigns` 结果——某些出口返回 plus 试用资格，另一些返回空。
通过 Clash API 切换节点重查，最多 N 次直到找到返回试用资格的出口。

用法::

    # 单个账号
    python scripts/find_trial_exit.py --email you@icloud.com --proxy 127.0.0.1:7897

    # 全部账号批量查找
    python scripts/find_trial_exit.py --all --proxy 127.0.0.1:7897
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gpt-auto-register"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MAX_ROTATIONS = 6       # 最多换几次出口
ROTATE_SLEEP = 1.5      # 每次切换间隔


def check_via_browser(driver, email: str) -> dict:
    """在当前节点下通过浏览器内页 fetch 查权益（最可靠通道）。"""
    from app.account_probe import probe_in_browser

    return probe_in_browser(driver, email=email, log=lambda *_: None)


def check_via_token(email: str, proxy: dict | None) -> dict:
    """用本地已存的 accessToken 通过 HTTP 查权益（回退通道）。"""
    from app.account_checker import probe_account_state

    return probe_account_state(email, proxy=proxy, timeout=15)


def main() -> int:
    parser = argparse.ArgumentParser(description="通过节点轮换查找 Plus 试用资格")
    parser.add_argument("--email", default="")
    parser.add_argument("--all", action="store_true", help="处理全部 Free 且未确认试用的账号")
    parser.add_argument("--proxy", default="127.0.0.1:7897")
    parser.add_argument("--max-rotate", type=int, default=MAX_ROTATIONS)
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()

    from app.browser import create_driver
    from app.account_perfector import _restore_saved_session
    from app.config import cfg, PROJECT_ROOT
    from app.stored_accounts import (
        load_accounts_from_file, update_account_check_result_in_file,
    )
    sys.path.insert(0, str(ROOT / "scripts"))
    from clash_rotate import rotate, find_selector
    from app.account_checker import save_state_evidence, state_evidence_summary

    accounts_file = Path(cfg.files.accounts_file)
    if not accounts_file.is_absolute():
        accounts_file = PROJECT_ROOT / accounts_file
    records = load_accounts_from_file(str(accounts_file)) if accounts_file.exists() else []

    clash_api = "http://127.0.0.1:9097"
    clash_secret = "key_1010"
    proxy_dict = None
    if args.proxy and args.proxy.lower() not in ("off", "none"):
        host, _, port = args.proxy.partition(":")
        proxy_dict = {"enabled": True, "type": "http", "host": host,
                      "port": int(port or 7897), "use_auth": False}

    if args.email:
        targets = [args.email]
    elif args.all:
        targets = [
            r["email"] for r in records
            if r.get("plan") in ("Free", "未检测")
            and not r.get("trial_eligible")
            and r.get("status") and r["status"] != "邮箱已创建"
        ]
    else:
        targets = []
    if not targets:
        print("没有需要查找试用资格的账号")
        return 0

    print(f"🎯 试用资格出口轮换查找 | 账号数: {len(targets)} | 最大轮换: {args.max_rotate}")
    print("=" * 70)

    driver = None
    results = []
    try:
        for index, email in enumerate(targets, 1):
            print(f"\n{'#'*70}\n# [{index}/{len(targets)}] {email}\n{'#'*70}")
            record = next((r for r in records if r["email"] == email), {})
            key = (record.get("two_factor_secret") or "").strip()

            found = False
            best_state = None
            rotations = 0

            for rotation in range(1, args.max_rotate + 1):
                if rotation > 1:
                    # 切换节点
                    try:
                        new_node = rotate(clash_api, clash_secret)
                        print(f"  🔄 节点轮换 #{rotation}: → {new_node}")
                        time.sleep(ROTATE_SLEEP)
                        rotations += 1
                    except Exception as exc:  # noqa: BLE001
                        print(f"  ⚠️ 节点切换失败: {exc}")
                        time.sleep(3)
                        continue

                # 通道选择：第1次用浏览器（cookie恢复），后续用token HTTP
                if rotation == 1 and driver is None:
                    driver = create_driver(headless=args.headless, proxy=proxy_dict)
                    restored = _restore_saved_session(driver, email, print)
                    if restored:
                        state = check_via_browser(driver, email)
                    else:
                        state = check_via_token(email, proxy_dict)
                else:
                    state = check_via_token(email, proxy_dict)

                eligible = state.get("trial_eligible")
                plan = state.get("plan", "?")
                print(f"  🔍 轮#{rotation}: 计划={plan} 试用={state.get('trial_status')} "
                      f"eligible={eligible}")

                if eligible is True:
                    print(f"  🎉 找到 Plus 试用资格！（第 {rotation} 次出口）")
                    found = True
                    best_state = state
                    break
                if state.get("verified"):
                    best_state = state  # 保留最完整的"无资格"结论作为兜底
                if eligible is False and rotations == 0:
                    # 第一次就明确返回"无"，也保存但继续轮换找"有"
                    best_state = state

            # 落库
            if best_state:
                evidence_file = save_state_evidence(email, best_state)
                update = {"plan": best_state.get("plan", "未检测"),
                          "trial_status": best_state.get("trial_status", "待官方确认"),
                          "trial_eligible": best_state.get("trial_eligible"),
                          "is_plus": best_state.get("is_plus"),
                          "is_paid": best_state.get("is_paid"),
                          "expires_at": best_state.get("expires_at"),
                          "account_status": best_state.get("account_status"),
                          "verified": best_state.get("verified", False)}
                try:
                    from app.stored_accounts import update_account_check_result_in_file as _upd
                    ev = state_evidence_summary(best_state, evidence_file)
                    _upd(str(accounts_file), email, update["plan"], update["trial_status"],
                         quota=update["quota"], expires_at=update["expires_at"],
                         account_status=update["account_status"],
                         two_factor_secret=key or None, evidence=ev)
                except Exception as exc:  # noqa: BLE001
                    print(f"  ⚠️ 写回失败: {exc}")

            results.append({
                "email": email,
                "trial_found": found,
                "rotations": rotations,
                "trial_status": best_state.get("trial_status") if best_state else "?",
                "plan": best_state.get("plan") if best_state else "?",
                "is_plus": best_state.get("is_plus") if best_state else None,
            })

            if found:
                print(f"  💾 已写回: 试用资格=✅")
            else:
                print(f"  💾 {args.max_rotate} 次出口均未找到试用资格")

            if index < len(targets):
                time.sleep(3)

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    found_count = sum(1 for r in results if r.get("trial_found"))
    print(f"\n📊 最终结果: {found_count}/{len(results)} 找到试用资格")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
