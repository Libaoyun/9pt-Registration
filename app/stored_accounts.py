"""
读取和查询已保存账号记录。
兼容 12 字段账号记录；检测结果可附加第 13 字段 JSON 证据：
邮箱|密码|时间|状态|临时邮箱凭证|提供商|订阅状态|试用资格|额度说明|会员到期时间|账号健康状态|2FA密钥(Base32)
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import contextlib
import os
import sys
import time

OAUTH_SUCCESS_STATUS = "已注册/OAuth成功"
_file_lock = threading.Lock()


@contextlib.contextmanager
def _interprocess_lock(lock_path: str):
    """跨进程锁：在 threading.Lock 基础上保障双版本并发写入安全"""
    p = str(lock_path) + ".lock"
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    except Exception:
        pass
    f = None
    try:
        f = open(p, "a+b")
        if sys.platform == "win32":
            import msvcrt
            for _ in range(40):
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if f:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass


def format_timestamp(raw_ts: str) -> str:
    """标准化时间字符串显示，例如 20261006_120000 转换为 2026-10-06 12:00:00"""
    if not raw_ts:
        return ""
    if len(raw_ts) == 15 and raw_ts[8] == "_":
        try:
            dt = datetime.strptime(raw_ts, "%Y%m%d_%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return raw_ts


def default_quota_for_plan(plan: str) -> str:
    """A plan name cannot establish the current model quota."""
    return "未知，请在官方页面确认"


def default_expiry_for_plan(plan: str, trial_status: str) -> str:
    """Neither plan nor trial labels establish an expiry date."""
    return "未知，未取得订阅到期时间"


def is_trial_eligible(trial_status: str) -> bool:
    """Legacy inferred labels are not proof of trial eligibility."""
    s = (trial_status or "").strip()
    return s.startswith("已确认有试用资格") or s == "已确认有试用资格"

def derive_health_status(raw_status: str, account_status: str | None = None) -> str:
    """推导账号健康运行状态"""
    if account_status and account_status.strip():
        return account_status.strip()
    s = (raw_status or "").lower()
    if "封禁" in s or "deactivated" in s:
        return "🚫 已封禁/停用"
    if "失效" in s or "expired" in s or "401" in s:
        return "⚠️ Token失效"
    if "失败" in s or "错误" in s:
        return "❌ 异常/失败"
    if "成功" in s or "已注册" in s or "oauth" in s:
        return "⚪ 已注册(未在线验证)"
    return "⚪ 待检测"


def load_accounts_from_file(file_path: str) -> list[dict]:
    """读取账号文件并转换为结构化字典列表，向下兼容任意历史格式"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"账号文件不存在: {file_path}")

    with _file_lock:
        text = path.read_text(encoding="utf-8", errors="replace")

    records = []
    for line in text.splitlines():
        # 兼容被 BOM 污染的邮箱（历史上用带 BOM 的 UTF-8 写文件会污染首行字段）
        line = line.lstrip("\ufeff")
        parts = line.strip().split("|")
        if len(parts) < 2 or "@" not in parts[0]:
            continue
        parts[0] = parts[0].lstrip("\ufeff").strip()

        raw_time = parts[2].strip() if len(parts) > 2 else ""
        raw_status = parts[3].strip() if len(parts) > 3 else "已注册"
        mailbox_cred = parts[4].strip() if len(parts) > 4 else ""
        provider = parts[5].strip() if len(parts) > 5 and parts[5].strip() else "mailtm"

        plan = parts[6].strip() if len(parts) > 6 and parts[6].strip() else "未检测"
        trial_status = parts[7].strip() if len(parts) > 7 and parts[7].strip() else "未检测"

        quota = (
            parts[8].strip()
            if len(parts) > 8 and parts[8].strip()
            else default_quota_for_plan(plan)
        )
        expires_at = (
            parts[9].strip()
            if len(parts) > 9 and parts[9].strip()
            else default_expiry_for_plan(plan, trial_status)
        )
        account_status = (
            parts[10].strip()
            if len(parts) > 10 and parts[10].strip()
            else derive_health_status(raw_status)
        )
        two_factor_secret = parts[11].strip() if len(parts) > 11 else ""
        evidence = {}
        if len(parts) > 12:
            try:
                value = json.loads(parts[12])
                if isinstance(value, dict):
                    evidence = value
            except (ValueError, TypeError):
                pass

        register_mode = str(evidence.get("register_mode", "")).strip().lower()
        if not register_mode:
            if provider == "import" or "导入" in raw_status or "import" in raw_status.lower():
                register_mode = "import"
            elif "protocol" in provider.lower() or evidence.get("source") == "protocol" or "protocol" in raw_status.lower() or "协议" in raw_status:
                register_mode = "protocol"
            else:
                register_mode = "browser"

        records.append(
            {
                "email": parts[0].strip(),
                "password": parts[1].strip(),
                "timestamp": format_timestamp(raw_time),
                "raw_timestamp": raw_time,
                "status": raw_status,
                "mailbox_credential": mailbox_cred,
                "provider": provider,
                "plan": plan,
                "trial_status": trial_status,
                "is_paid": evidence.get("verified") is True and evidence.get("is_paid") is True,
                "is_plus": evidence.get("is_plus") is True,
                "trial_eligible": evidence.get("trial_eligible") is True,
                "checked_at": evidence.get("checked_at", ""),
                "sources": evidence.get("sources", []),
                "evidence_file": evidence.get("evidence_file", ""),
                "verified": evidence.get("verified") is True,
                "profile_evidence": evidence,
                "quota": quota,
                "expires_at": expires_at,
                "account_status": account_status,
                "two_factor_secret": two_factor_secret,
                "register_mode": register_mode,
            }
        )
    return records


def load_account_from_file(file_path: str, email: str) -> dict:
    for record in load_accounts_from_file(file_path):
        if record["email"] == email:
            return record

    raise ValueError(f"未在账号文件中找到邮箱: {email}")


def is_oauth_success_status(status: str) -> bool:
    return (status or "").strip() == OAUTH_SUCCESS_STATUS


def update_account_status_in_file(
    file_path: str,
    email: str,
    new_status: str,
    plan: str | None = None,
    trial_status: str | None = None,
    quota: str | None = None,
    expires_at: str | None = None,
    account_status: str | None = None,
    two_factor_secret: str | None = None,
) -> None:
    """更新账号注册/Token状态与资格详情"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"账号文件不存在: {file_path}")

    current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _file_lock:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        updated_lines = []
        found = False

        for line in lines:
            parts = line.strip().split("|")
            if len(parts) < 2 or "@" not in parts[0]:
                updated_lines.append(line)
                continue

            if parts[0].strip() != email:
                updated_lines.append(line)
                continue

            password = parts[1].strip() if len(parts) > 1 else "N/A"
            mailbox_credential = parts[4].strip() if len(parts) > 4 else ""
            provider = parts[5].strip() if len(parts) > 5 else "mailtm"
            current_plan = plan if plan is not None else (parts[6].strip() if len(parts) > 6 else "未检测")
            current_trial = trial_status if trial_status is not None else (parts[7].strip() if len(parts) > 7 else "未检测")
            current_quota = quota if quota is not None else (parts[8].strip() if len(parts) > 8 else default_quota_for_plan(current_plan))
            current_expiry = expires_at if expires_at is not None else (parts[9].strip() if len(parts) > 9 else default_expiry_for_plan(current_plan, current_trial))
            current_health = account_status if account_status is not None else (parts[10].strip() if len(parts) > 10 else derive_health_status(new_status))
            current_2fa = two_factor_secret if two_factor_secret is not None else (parts[11].strip() if len(parts) > 11 else "")

            updated_lines.append(
                f"{email}|{password}|{current_date}|{new_status}|{mailbox_credential}|{provider}|{current_plan}|{current_trial}|{current_quota}|{current_expiry}|{current_health}|{current_2fa}"
            )
            found = True

        if not found:
            raise ValueError(f"未在账号文件中找到邮箱: {email}")

        path.write_text("".join(f"{line}\n" for line in updated_lines), encoding="utf-8")


def update_account_check_result_in_file(
    file_path: str,
    email: str,
    plan: str,
    trial_status: str,
    quota: str | None = None,
    expires_at: str | None = None,
    account_status: str | None = None,
    two_factor_secret: str | None = None,
    evidence: dict | None = None,
) -> None:
    """更新账号在线检测画像（计划、试用资格、额度、到期时间、健康状态、2FA密钥）"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"账号文件不存在: {file_path}")

    with _file_lock, _interprocess_lock(file_path):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        updated_lines = []
        found = False

        for line in lines:
            parts = line.strip().split("|")
            if len(parts) < 2 or "@" not in parts[0]:
                updated_lines.append(line)
                continue

            if parts[0].strip() != email:
                updated_lines.append(line)
                continue

            password = parts[1].strip() if len(parts) > 1 else "N/A"
            timestamp = parts[2].strip() if len(parts) > 2 else ""
            status = parts[3].strip() if len(parts) > 3 else "已注册"
            mailbox_credential = parts[4].strip() if len(parts) > 4 else ""
            provider = parts[5].strip() if len(parts) > 5 else "mailtm"

            final_quota = quota if quota is not None else (parts[8].strip() if len(parts) > 8 else default_quota_for_plan(plan))
            final_expiry = expires_at if expires_at is not None else (parts[9].strip() if len(parts) > 9 else default_expiry_for_plan(plan, trial_status))
            final_health = account_status if account_status is not None else (parts[10].strip() if len(parts) > 10 else derive_health_status(status))
            final_2fa = two_factor_secret if two_factor_secret is not None else (parts[11].strip() if len(parts) > 11 else "")

            record_line = f"{email}|{password}|{timestamp}|{status}|{mailbox_credential}|{provider}|{plan}|{trial_status}|{final_quota}|{final_expiry}|{final_health}|{final_2fa}"
            if evidence is not None:
                record_line += "|" + json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
            updated_lines.append(record_line)
            found = True

        if not found:
            raise ValueError(f"未在账号文件中找到邮箱: {email}")

        path.write_text("".join(f"{line}\n" for line in updated_lines), encoding="utf-8")


def is_registered_status(status: str) -> bool:
    """检查状态是否代表已成功注册（非失败、非临时创建中）"""
    s = (status or "").strip().lower()
    if not s:
        return False
    if "失败" in s or "错误" in s or "超时" in s or "banned" in s:
        return False
    if s in ["邮箱已创建", "待检测", "创建中"]:
        return False
    if any(k in s for k in ["注册", "oauth", "成功", "导入", "在线已验证", "active", "跳过"]):
        return True
    return False


def get_registered_account_by_email(file_path: str, email: str) -> dict | None:
    """按邮箱查找已存储的账号记录，不抛出异常"""
    if not email:
        return None
    try:
        email_clean = email.strip().lower()
        for record in load_accounts_from_file(file_path):
            if record.get("email", "").strip().lower() == email_clean:
                return record
    except Exception:
        pass
    return None


def parse_imported_account_line(line: str) -> dict | None:
    """智能解析导入的账号单行，支持 5横杠/4横杠/管道符/冒号及纯邮箱等格式"""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None

    email = ""
    password = ""
    two_factor = ""
    plan = "未检测"
    trial_status = "待官方确认"
    quota = "未知，请在官方页面确认"
    expires_at = "未知，未取得订阅到期时间"
    status = "已注册"
    account_status = "⚪ 未在线验证"
    provider = "import"
    mailbox_cred = ""

    if "-----" in raw:
        parts = [p.strip() for p in raw.split("-----")]
        email = parts[0]
        if len(parts) > 1:
            password = parts[1]
        if len(parts) > 2:
            two_factor = parts[2]
    elif "----" in raw:
        parts = [p.strip() for p in raw.split("----")]
        email = parts[0]
        if len(parts) > 1:
            password = parts[1]
        if len(parts) > 2:
            plan = parts[2]
        if len(parts) > 3:
            trial_status = parts[3]
        if len(parts) > 4:
            quota = parts[4]
        if len(parts) > 5:
            expires_at = parts[5]
        if len(parts) > 6:
            account_status = parts[6]
        if len(parts) > 7:
            two_factor = parts[7]
    elif "|" in raw:
        parts = [p.strip() for p in raw.split("|")]
        email = parts[0]
        if len(parts) > 1:
            password = parts[1]
        if len(parts) > 3:
            status = parts[3] or "已注册"
        if len(parts) > 4:
            mailbox_cred = parts[4]
        if len(parts) > 5:
            provider = parts[5] or "import"
        if len(parts) > 6:
            plan = parts[6] or "未检测"
        if len(parts) > 7:
            trial_status = parts[7] or "待官方确认"
        if len(parts) > 8:
            quota = parts[8]
        if len(parts) > 9:
            expires_at = parts[9]
        if len(parts) > 10:
            account_status = parts[10]
        if len(parts) > 11:
            two_factor = parts[11]
    elif ":" in raw and "@" in raw:
        parts = [p.strip() for p in raw.split(":", 1)]
        email = parts[0]
        password = parts[1]
    elif "," in raw and "@" in raw:
        parts = [p.strip() for p in raw.split(",", 1)]
        email = parts[0]
        password = parts[1]
    else:
        email = raw

    if not email or "@" not in email:
        return None

    return {
        "email": email,
        "password": password or "N/A",
        "two_factor_secret": two_factor,
        "status": status,
        "plan": plan,
        "trial_status": trial_status,
        "quota": quota,
        "expires_at": expires_at,
        "account_status": account_status,
        "provider": provider,
        "mailbox_credential": mailbox_cred,
    }


def import_accounts_from_text(file_path: str, text: str) -> dict:
    """批量导入账号文本，若已存在且已注册则跳过/保留，若为新账号则录入并展示在表格中"""
    if not text or not text.strip():
        return {"success": True, "total": 0, "imported": 0, "skipped": 0, "message": "无导入内容"}

    existing_records = {}
    path = Path(file_path)
    if path.exists():
        for r in load_accounts_from_file(file_path):
            existing_records[r["email"].lower()] = r

    lines = text.strip().splitlines()
    total = len(lines)
    imported = 0
    skipped = 0

    from .utils import save_to_txt

    for line in lines:
        item = parse_imported_account_line(line)
        if not item:
            continue
        em_lower = item["email"].lower()
        if em_lower in existing_records:
            exist = existing_records[em_lower]
            if is_registered_status(exist.get("status")):
                # 已注册过，直接跳过注册，表格依然完整展示该账号
                skipped += 1
                continue

        # 录入到 registered_accounts.txt，确保在表格中完整展示
        save_to_txt(
            email=item["email"],
            password=item["password"],
            status=item.get("status", "已注册"),
            mailtm_password=item.get("mailbox_credential", ""),
            provider=item.get("provider", "import"),
            plan=item.get("plan"),
            trial_status=item.get("trial_status"),
            quota=item.get("quota"),
            expires_at=item.get("expires_at"),
            account_status=item.get("account_status"),
            two_factor_secret=item.get("two_factor_secret"),
            evidence={"register_mode": "import"},
        )
        existing_records[em_lower] = item
        imported += 1

    return {
        "success": True,
        "total": total,
        "imported": imported,
        "skipped": skipped,
        "message": f"成功导入 {imported} 个账号，跳过已存在注册账号 {skipped} 个",
    }
