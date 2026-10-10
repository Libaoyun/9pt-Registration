"""全自动流水线：注册 → 补全 → 检测 → 出报表。

四个阶段（每个阶段都会写实时日志与进度，面板可直接观察）:

1. ``register``  批量注册账号（浏览器自动化 / 极速协议，两种模式共用本编排）
2. ``perfect``   对缺少 Web 凭据 / 未确认 2FA / 权益未知的账号做真实补全：
                 登录 → 抓取真实 Web access token → 官方确认后绑定真实 TOTP 2FA →
                 官方接口采集权益（1 个月 Plus 试用资格、是否 Plus、到期时间）
3. ``verify``    对账号做一次全量真实状态刷新（可只覆盖本轮新增账号）
4. ``report``    产出最终数据集与文件：账号-密码-动态2FA-注册时间-试用资格-Plus 状态

所有阶段都不会凭空写入数据：拿不到官方返回就保持"未知"，并在证据里留下原因。
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .account_checker import refresh_account_state
from .config import cfg, PROJECT_ROOT
from .stored_accounts import is_registered_status, load_accounts_from_file
from .two_factor_service import generate_totp_code, totp_seconds_remaining

_TZ = timezone(timedelta(hours=8))

PHASES = ("register", "perfect", "verify", "report")
PHASE_LABELS = {
    "register": "① 批量注册",
    "perfect": "② 真实补全 (Web凭据/2FA)",
    "verify": "③ 官方接口全量检测",
    "report": "④ 生成最终报表",
}


def _now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def accounts_file_path() -> Path:
    return _resolve_path(cfg.files.accounts_file)


def _hash_2fa(secret: str) -> str:
    return (secret or "").strip()


def _resolve_register_func(mode: str):
    if mode == "protocol":
        from .protocol_register import register_one_account

        return register_one_account
    from .main import register_one_account

    return register_one_account


def _resolve_perfect_func():
    from .account_perfector import perfect_single_account

    return perfect_single_account


def _needs_perfect(record: dict) -> bool:
    """判断账号是否还需要补全（缺 2FA / 缺在线证据 / 权益未知）。"""
    if not is_registered_status(record.get("status", "")):
        return False
    secret = _hash_2fa(record.get("two_factor_secret", ""))
    if not secret or secret in ("未开启", "N/A"):
        return True
    if not record.get("verified"):
        return True
    if record.get("is_plus") is None and record.get("trial_eligible") is None:
        return True
    return False


class AutoPipeline:
    """注册 → 补全 → 检测 → 报表 的编排器。"""

    def __init__(
        self,
        *,
        mode: str = "auto",
        log: Callable[[str], None] = print,
        stop_check: Callable[[], bool] | None = None,
        phase_callback: Callable[[str, int, int, str], None] | None = None,
    ):
        self.mode = mode
        self.log = log
        self.stop_check = stop_check or (lambda: False)
        self.phase_callback = phase_callback or (lambda *_: None)
        self.current_phase = "idle"
        self.report: dict = {}

    # ---------------------------------------------------------------- utils
    def _stopped(self) -> bool:
        return bool(self.stop_check())

    def _set_phase(self, phase: str, done: int = 0, total: int = 0, message: str = "") -> None:
        self.current_phase = phase
        self.phase_callback(phase, done, total, message)

    def _select_mode(self) -> str:
        if self.mode in ("browser", "protocol"):
            return self.mode
        try:
            from . import protocol_register  # noqa: F401

            return "protocol"
        except Exception:
            return "browser"

    # ------------------------------------------------------------ phase 1
    def phase_register(self, count: int, providers: list[str], parallel: int,
                       headless: bool, proxy: dict | None, referral_url: str,
                       enable_2fa: bool, stagger_seconds: int = 0) -> dict:
        mode = self._select_mode()
        register_func = _resolve_register_func(mode)
        self._set_phase("register", 0, count, f"正在以 {mode} 模式注册 {count} 个账号")
        self.log(f"🚀 [全自动] 阶段① 批量注册开始 | 模式: {mode} | 目标: {count} | 并发: {parallel}"
                 + (f" | 间隔: {stagger_seconds}s" if stagger_seconds else ""))

        result = {"total": count, "success": 0, "fail": 0, "skipped": 0, "emails": [], "mode": mode}
        lock = threading.Lock()

        def _one(index: int):
            if self._stopped():
                return None
            email_provider = providers[(index - 1) % len(providers)]
            try:
                outcome = register_func(
                    email_provider=email_provider,
                    headless=headless,
                    proxy=proxy,
                    referral_url=referral_url,
                    enable_2fa=enable_2fa,
                )
            except Exception as exc:  # noqa: BLE001
                self.log(f"❌ 第 {index} 个账号注册异常: {exc}")
                return None
            email = outcome[0] if len(outcome) >= 1 else None
            success = bool(outcome[2]) if len(outcome) >= 3 else False
            skipped = bool(getattr(outcome, "skipped", False))
            with lock:
                if skipped:
                    result["skipped"] += 1
                    result["success"] += 1
                elif success:
                    result["success"] += 1
                else:
                    result["fail"] += 1
                if email:
                    result["emails"].append(email)
                done = result["success"] + result["fail"]
            self._set_phase("register", done, count, f"注册进度 {done}/{count}")
            return outcome

        workers = max(1, min(parallel, max(1, count)))
        if workers == 1:
            for index in range(1, count + 1):
                if self._stopped():
                    break
                _one(index)
                # 注册节奏控制：放慢可显著降低 IP 被风控的概率
                if stagger_seconds > 0 and index < count:
                    self.log(f"   ⏳ 节奏控制：等待 {stagger_seconds}s 后再注册下一个 ...")
                    for _ in range(stagger_seconds):
                        if self._stopped():
                            break
                        time.sleep(1)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = []
                for index in range(1, count + 1):
                    futures.append(executor.submit(_one, index))
                    if stagger_seconds > 0 and index < count:
                        time.sleep(stagger_seconds)  # 错峰启动，避免同时打爆同一个 IP
                for future in as_completed(futures):
                    if self._stopped():
                        break
                    try:
                        future.result()
                    except Exception:
                        pass

        self.log(
            f"🏁 [全自动] 阶段① 完成 | 成功: {result['success']} (跳过已注册 {result['skipped']}) | 失败: {result['fail']}"
        )
        return result

    # ------------------------------------------------------------ phase 2
    def phase_perfect(self, emails: list[str], proxy: dict | None,
                      headless: bool = True, workers: int = 2) -> dict:
        perfect_func = _resolve_perfect_func()
        targets: list[str] = []
        records: dict[str, dict] = {}
        path = accounts_file_path()
        if path.exists():
            try:
                records = {record["email"]: record for record in load_accounts_from_file(str(path))}
            except Exception as exc:  # noqa: BLE001
                self.log(f"⚠️ 读取账号文件失败，按全部补全处理: {exc}")
                records = {}
        for email in emails:
            record = records.get(email)
            if record is None or _needs_perfect(record):
                targets.append(email)

        summary = {"total": len(targets), "success": 0, "fail": 0, "results": []}
        self._set_phase("perfect", 0, len(targets), f"待补全账号: {len(targets)}")
        if not targets:
            self.log("✅ [全自动] 阶段② 无需补全，所有账号已有真实 2FA 与在线证据")
            return summary

        self.log(f"⚡ [全自动] 阶段② 真实补全开始 | 目标 {len(targets)} 个账号 | 并发 {workers}")
        lock = threading.Lock()
        done = 0

        def _one(email: str) -> dict:
            if self._stopped():
                return {"email": email, "success": False, "error": "stopped"}
            try:
                return perfect_func(
                    email=email,
                    proxy=proxy,
                    headless=headless,
                    log_func=lambda message: self.log(message),
                )
            except Exception as exc:  # noqa: BLE001
                self.log(f"❌ 补全 {email} 异常: {exc}")
                return {"email": email, "success": False, "error": str(exc)}

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets)))) as executor:
            futures = {executor.submit(_one, email): email for email in targets}
            for future in as_completed(futures):
                result = future.result() if not future.exception() else {
                    "email": futures[future], "success": False, "error": str(future.exception())
                }
                summary["results"].append(result)
                summary["success" if result.get("success") else "fail"] += 1
                with lock:
                    done += 1
                self._set_phase("perfect", done, len(targets), f"补全进度 {done}/{len(targets)}")
        self.log(f"🏁 [全自动] 阶段② 完成 | 成功: {summary['success']} | 失败: {summary['fail']}")
        return summary

    # ------------------------------------------------------------ phase 3
    def phase_verify(self, emails: list[str], proxy: dict | None, workers: int = 3) -> dict:
        accounts_file = str(accounts_file_path())
        summary = {"total": len(emails), "verified": 0, "unknown": 0}
        self._set_phase("verify", 0, len(emails), f"待在线检测: {len(emails)}")
        if not emails:
            return summary

        self.log(f"🔍 [全自动] 阶段③ 官方接口全量检测开始 | 目标 {len(emails)} 个账号")
        done = 0
        lock = threading.Lock()

        def _one(email: str) -> dict:
            if self._stopped():
                return {"email": email, "verified": False}
            try:
                return refresh_account_state(email, accounts_file, proxy=proxy)
            except Exception as exc:  # noqa: BLE001
                self.log(f"⚠️ 检测 {email} 失败: {exc}")
                return {"email": email, "verified": False, "error": str(exc)}

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(emails)))) as executor:
            futures = {executor.submit(_one, email): email for email in emails}
            for future in as_completed(futures):
                email = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result = {"email": email, "verified": False, "error": str(exc)}
                if result.get("verified"):
                    summary["verified"] += 1
                else:
                    summary["unknown"] += 1
                with lock:
                    done += 1
                self._set_phase("verify", done, len(emails), f"检测进度 {done}/{len(emails)}")
        self.log(
            f"🏁 [全自动] 阶段③ 完成 | 官方确认: {summary['verified']} | 未取得证据: {summary['unknown']}"
        )
        return summary

    # ------------------------------------------------------------ phase 4
    def run(self, *, count: int = 1, providers: list[str] | None = None, parallel: int = 1,
            headless: bool = False, proxy: dict | None = None, referral_url: str = "",
            enable_2fa: bool = True, verify_all: bool = False, perfect_workers: int = 2,
            stagger_seconds: int = 0, skip_completion: bool = False) -> dict:
        """执行全自动流水线。

        ``stagger_seconds``  注册间隔（秒）：放慢节奏可显著降低 IP 被风控的概率。
        ``skip_completion``  纯极速模式：跳过浏览器补全阶段（不抓 Web 凭据 / 不绑 2FA），
                             只做"注册 + 检测"，适合协议版批量铺号。
        """
        providers = providers or list(getattr(cfg, "default_providers", None) or ["mailtm"])
        started_at = datetime.now(_TZ)
        self.log("=" * 60)
        self.log(f"🤖 全自动流水线启动 | 模式: {self._select_mode()} | 目标账号: {count}")
        self.log("=" * 60)

        register_summary = self.phase_register(
            count=count, providers=providers, parallel=parallel, headless=headless,
            proxy=proxy, referral_url=referral_url, enable_2fa=enable_2fa,
            stagger_seconds=stagger_seconds,
        )
        new_emails = register_summary.get("emails", [])

        if skip_completion:
            self.log("⏩ 纯极速模式：跳过浏览器补全（不抓 Web 凭据 / 不绑 2FA）")
            perfect_summary = {"total": 0, "success": 0, "fail": 0, "results": []}
        else:
            perfect_summary = self.phase_perfect(new_emails, proxy=proxy, headless=True, workers=perfect_workers)

        if verify_all:
            try:
                all_emails = [r["email"] for r in load_accounts_from_file(str(accounts_file_path()))]
            except Exception:
                all_emails = new_emails
        else:
            all_emails = sorted(set(new_emails))
        verify_summary = self.phase_verify(all_emails, proxy=proxy, workers=max(2, perfect_workers))

        report = self.build_report(verify_scope="all" if verify_all else "new", new_emails=new_emails)
        files = write_report_files(report)
        report["files"] = files
        report["started_at"] = started_at.isoformat(timespec="seconds")
        report["finished_at"] = datetime.now(_TZ).isoformat(timespec="seconds")
        report["summary"] = {
            "mode": self._select_mode(),
            "requested": count,
            "registered_ok": register_summary["success"],
            "registered_fail": register_summary["fail"],
            "skipped_existing": register_summary["skipped"],
            "perfect_ok": perfect_summary["success"],
            "perfect_fail": perfect_summary["fail"],
            "verified": verify_summary["verified"],
            "unverified": verify_summary["unknown"],
            "with_2fa": sum(1 for row in report["rows"] if row.get("two_factor_secret")),
            "plus_active": sum(1 for row in report["rows"] if row.get("is_plus")),
            "trial_eligible": sum(1 for row in report["rows"] if row.get("trial_eligible")),
            "files": files,
        }
        self.report = report
        self._set_phase("report", len(report["rows"]), len(report["rows"]), "报表已生成")
        self.log(
            f"🎉 [全自动] 流水线完成 | 注册成功 {register_summary['success']} | 补全成功 "
            f"{perfect_summary['success']} | 官方确认 {verify_summary['verified']} | 含2FA "
            f"{report['summary']['with_2fa']} | Plus {report['summary']['plus_active']} | "
            f"有试用资格 {report['summary']['trial_eligible']}"
        )
        for name, path in files.items():
            self.log(f"   📄 {name}: {path}")
        return report

    # ------------------------------------------------------------ report
    def build_report(self, *, verify_scope: str = "all", new_emails: list[str] | None = None) -> dict:
        accounts_file = str(accounts_file_path())
        records = load_accounts_from_file(accounts_file) if os.path.exists(accounts_file) else []
        new_set = {email.lower() for email in (new_emails or [])}
        rows: list[dict] = []
        for record in records:
            if verify_scope == "new" and new_set and record["email"].lower() not in new_set:
                continue
            secret = _hash_2fa(record.get("two_factor_secret", ""))
            has_secret = bool(secret) and secret not in ("未开启", "N/A")
            rows.append({
                "email": record["email"],
                "password": record.get("password", "N/A"),
                "two_factor_secret": secret if has_secret else "",
                "two_factor_now": generate_totp_code(secret) if has_secret else "",
                "two_factor_remaining": totp_seconds_remaining() if has_secret else 0,
                "otpauth_url": (
                    f"otpauth://totp/OpenAI:{record['email']}?secret={secret}&issuer=OpenAI"
                    if has_secret else ""
                ),
                "register_time": record.get("timestamp", ""),
                "status": record.get("status", ""),
                "register_mode": record.get("register_mode", ""),
                "provider": record.get("provider", ""),
                "mailbox_credential": record.get("mailbox_credential", ""),
                "plan": record.get("plan", "未检测"),
                "is_plus": record.get("is_plus"),
                "is_paid": record.get("is_paid"),
                "trial_status": record.get("trial_status", "待官方确认"),
                "trial_eligible": record.get("trial_eligible"),
                "quota": record.get("quota", ""),
                "expires_at": record.get("expires_at", ""),
                "account_status": record.get("account_status", ""),
                "verified": bool(record.get("verified")),
                "checked_at": record.get("checked_at", ""),
                "sources": record.get("sources", []),
                "evidence_file": record.get("evidence_file", ""),
            })
        rows.sort(key=lambda row: row.get("register_time", ""), reverse=True)
        return {
            "generated_at": datetime.now(_TZ).isoformat(timespec="seconds"),
            "accounts_file": accounts_file,
            "scope": verify_scope,
            "total": len(rows),
            "rows": rows,
        }


def write_report_files(report: dict, output_dir: str | None = None) -> dict:
    """把最终报表落盘：JSON 全量 + 5横杠 2FA 文本 + CSV + Markdown 表格。"""
    base = _resolve_path(output_dir or "token_exports") / f"final_{_now()}"
    base.mkdir(parents=True, exist_ok=True)
    rows = report.get("rows", [])

    json_path = base / "final_report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    txt_path = base / "accounts_2fa.txt"
    txt_lines = []
    for row in rows:
        secret = row.get("two_factor_secret") or "未开启2FA"
        txt_lines.append(f"{row['email']}-----{row.get('password') or 'N/A'}-----{secret}")
    txt_path.write_text("\n".join(txt_lines) + ("\n" if txt_lines else ""), encoding="utf-8")

    csv_path = base / "final_report.csv"
    fieldnames = [
        "email", "password", "two_factor_secret", "two_factor_now", "register_time",
        "trial_status", "trial_eligible", "plan", "is_plus", "is_paid", "expires_at",
        "quota", "account_status", "verified", "checked_at", "register_mode", "provider",
        "evidence_file",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    md_path = base / "final_report.md"
    lines = [
        "# 自动注册最终报表",
        "",
        f"- 生成时间: {report.get('generated_at')}",
        f"- 账号总数: {report.get('total')}",
        f"- 数据文件: `{report.get('accounts_file')}`",
        "",
        "| 邮箱 | 密码 | 2FA密钥 | 动态码 | 注册时间 | 试用资格 | Plus | 计划 | 到期时间 | 在线状态 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {email} | {password} | {secret} | {code} | {time} | {trial} | {plus} | {plan} | {expires} | {health} |".format(
                email=row.get("email", ""),
                password=row.get("password", "N/A"),
                secret=row.get("two_factor_secret") or "未开启2FA",
                code=row.get("two_factor_now") or "-",
                time=row.get("register_time", ""),
                trial=row.get("trial_status", ""),
                plus={True: "✅ Plus", False: "❌ 非Plus", None: "❔ 未知"}.get(row.get("is_plus")),
                plan=row.get("plan", ""),
                expires=row.get("expires_at", ""),
                health=row.get("account_status", ""),
            )
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "json": str(json_path),
        "accounts_2fa_txt": str(txt_path),
        "csv": str(csv_path),
        "markdown": str(md_path),
    }


def build_report_now(scope: str = "all") -> dict:
    """只构建报表（不跑注册），供面板/接口按需刷新。"""
    return AutoPipeline(log=lambda *_: None).build_report(verify_scope=scope)


def run_autopilot(*, count: int = 1, providers: list[str] | None = None, parallel: int = 1,
                  headless: bool = False, proxy: dict | None = None, referral_url: str = "",
                  enable_2fa: bool = True, mode: str = "auto", verify_all: bool = False,
                  stagger_seconds: int = 0, skip_completion: bool = False,
                  log: Callable[[str], None] = print) -> dict:
    """便捷入口（CLI / 协议版 CLI 使用）。"""
    pipeline = AutoPipeline(mode=mode, log=log)
    return pipeline.run(
        count=count, providers=providers, parallel=parallel, headless=headless, proxy=proxy,
        referral_url=referral_url, enable_2fa=enable_2fa, verify_all=verify_all,
        stagger_seconds=stagger_seconds, skip_completion=skip_completion,
    )
