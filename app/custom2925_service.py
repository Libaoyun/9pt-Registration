"""
2925 自有邮箱服务模块 - 基于共享 IMAP 收件箱的 alias 邮箱

工作方式：
  - 注册时生成 youraliasbN@2925.com 形式的别名地址
  - 所有邮件统一投递到主邮箱 your-main-mail@2925.com
  - 通过 IMAP 轮询主收件箱，并按 alias 精确匹配邮件后提取验证码
"""

from __future__ import annotations

import email
import imaplib
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

from .config import EMAIL_WAIT_TIMEOUT, EMAIL_POLL_INTERVAL, PROJECT_ROOT, cfg
from .utils import extract_verification_code

_OPENAI_KEYWORDS = ("openai", "noreply@openai", "chatgpt")
_HEADER_MATCH_FIELDS = ("Delivered-To", "X-Original-To", "To")
_counter_lock = threading.Lock()
_sessions_lock = threading.Lock()

_sessions: dict[str, dict] = {}


def _resolve_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _ensure_counter_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps({"next_index": cfg.custom2925.start_index}, ensure_ascii=False, indent=2), encoding="utf-8")


def _next_alias_index() -> int:
    counter_path = _resolve_path(cfg.custom2925.counter_file)
    with _counter_lock:
        _ensure_counter_file(counter_path)
        try:
            payload = json.loads(counter_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            payload = {}
        current = int(payload.get("next_index", cfg.custom2925.start_index))
        payload["next_index"] = current + 1
        counter_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return current


def _build_alias(index: int) -> str:
    prefix = (cfg.custom2925.alias_prefix or "").strip()
    separator = cfg.custom2925.alias_separator or ""
    domain = (cfg.custom2925.domain or "").strip()
    if not prefix or not domain:
        raise RuntimeError("custom2925 alias_prefix 或 domain 未配置")
    return f"{prefix}{separator}{index}@{domain}"


def _connect_imap():
    host = cfg.custom2925.imap_host.strip()
    port = int(cfg.custom2925.imap_port)
    username = cfg.custom2925.imap_user.strip()
    password = cfg.custom2925.imap_password
    mailbox = cfg.custom2925.mailbox or "INBOX"

    if not host or not username or not password:
        raise RuntimeError("custom2925 IMAP 配置不完整，请检查 host/user/password")

    client = imaplib.IMAP4_SSL(host, port) if cfg.custom2925.imap_ssl else imaplib.IMAP4(host, port)
    client.login(username, password)
    status, _ = client.select(mailbox)
    if status != "OK":
        client.logout()
        raise RuntimeError(f"选择邮箱文件夹失败: {mailbox}")
    return client


def _extract_message_text(msg: Message) -> str:
    texts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            if part.get_content_disposition() == "attachment":
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                raw = part.get_payload()
                if isinstance(raw, str):
                    texts.append(raw)
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                texts.append(payload.decode(charset, errors="ignore"))
            except Exception:
                texts.append(payload.decode("utf-8", errors="ignore"))
    else:
        payload = msg.get_payload(decode=True)
        if payload is None:
            raw = msg.get_payload()
            if isinstance(raw, str):
                texts.append(raw)
        else:
            charset = msg.get_content_charset() or "utf-8"
            try:
                texts.append(payload.decode(charset, errors="ignore"))
            except Exception:
                texts.append(payload.decode("utf-8", errors="ignore"))
    return "\n".join(filter(None, texts))


def _message_timestamp(msg: Message) -> datetime | None:
    raw_date = msg.get("Date")
    if not raw_date:
        return None
    try:
        dt = parsedate_to_datetime(raw_date)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _message_matches_alias(msg: Message, alias_email: str, created_after: datetime) -> bool:
    alias_lower = alias_email.lower()
    for field in _HEADER_MATCH_FIELDS:
        value = _decode_header_value(msg.get(field))
        if alias_lower in value.lower():
            return True

    subject = _decode_header_value(msg.get("Subject"))
    body = _extract_message_text(msg)
    haystack = f"{subject}\n{body}".lower()
    if alias_lower not in haystack:
        return False

    msg_time = _message_timestamp(msg)
    if msg_time is None:
        return True
    return msg_time >= created_after


def _message_looks_like_openai(msg: Message, body: str) -> bool:
    from_header = _decode_header_value(msg.get("From")).lower()
    subject = _decode_header_value(msg.get("Subject")).lower()
    body_lower = body.lower()
    return any(keyword in from_header or keyword in subject or keyword in body_lower for keyword in _OPENAI_KEYWORDS)


def _iter_recent_message_ids(client, limit: int = 40) -> list[tuple[str, bytes | str, bool]]:
    """返回最近邮件标识列表: (display_id, fetch_id, use_uid_fetch)。"""
    try:
        status, data = client.uid("search", None, "ALL")
        if status == "OK" and data and data[0]:
            uids = [uid for uid in data[0].split() if uid]
            if uids:
                return [(uid.decode(errors="ignore"), uid, True) for uid in reversed(uids[-limit:])]
    except Exception as e:
        print(f"  UID SEARCH 不可用，改用序号遍历: {e}")

    try:
        message_count = int(client.select()[1][0])
    except Exception as e:
        raise RuntimeError(f"无法获取收件箱邮件数量: {e}")

    start = max(1, message_count - limit + 1)
    ids = []
    for seq in range(message_count, start - 1, -1):
        seq_id = str(seq)
        ids.append((seq_id, seq_id, False))
    return ids


def _fetch_rfc822(client, fetch_id: bytes | str, use_uid_fetch: bool):
    if use_uid_fetch:
        return client.uid("fetch", fetch_id, "(RFC822)")
    return client.fetch(str(fetch_id), "(RFC822)")


def _fetch_matching_messages(alias_email: str, created_after: datetime, seen_uids: set[str]) -> list[tuple[str, Message, str]]:
    client = _connect_imap()
    try:
        identifiers = _iter_recent_message_ids(client, limit=40)
        matched: list[tuple[str, Message, str]] = []
        for display_id, fetch_id, use_uid_fetch in identifiers:
            if display_id in seen_uids:
                continue
            status, msg_data = _fetch_rfc822(client, fetch_id, use_uid_fetch)
            if status != "OK":
                continue
            raw_message = None
            for item in msg_data:
                if isinstance(item, tuple) and len(item) >= 2:
                    raw_message = item[1]
                    break
            if not raw_message:
                continue
            msg = email.message_from_bytes(raw_message)
            msg_time = _message_timestamp(msg)
            if msg_time and msg_time < created_after:
                continue
            body = _extract_message_text(msg)
            if not _message_looks_like_openai(msg, body):
                continue
            if not _message_matches_alias(msg, alias_email, created_after):
                continue
            matched.append((display_id, msg, body))
        return matched
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass


def _create_alias_session(alias_email: str, lookback_seconds: int | None = None) -> str:
    session_id = str(uuid.uuid4())
    lookback = max(int(cfg.custom2925.lookback_seconds if lookback_seconds is None else lookback_seconds), 0)
    created_at = datetime.now(timezone.utc) - timedelta(seconds=min(lookback, 30))

    with _sessions_lock:
        _sessions[session_id] = {
            "alias_email": alias_email,
            "created_at": created_at,
            "seen_uids": set(),
        }
    return session_id


def create_temp_email(proxy: dict = None):
    del proxy
    if not cfg.custom2925.enabled:
        print("⚠️ custom2925 未启用，但仍按配置生成 alias 邮箱")

    index = _next_alias_index()
    alias_email = _build_alias(index)
    session_id = _create_alias_session(alias_email)

    print(f"✅ 2925 alias 邮箱已生成: {alias_email}")
    return alias_email, session_id, cfg.custom2925.base_email


def login_existing_email(address: str, password: str | None = None):
    del password
    if "@" not in (address or ""):
        raise RuntimeError("custom2925 账号地址无效")
    session_id = _create_alias_session(address)
    print(f"✅ 已为 2925 别名邮箱创建收信会话: {address}")
    return session_id


def _extract_codes_from_messages(session: dict, messages: list[tuple[str, Message, str]], mark_seen: bool) -> list[str]:
    seen_codes = set()
    codes: list[str] = []
    for uid, msg, body in messages:
        subject = _decode_header_value(msg.get("Subject"))
        for content in (subject, body):
            code = extract_verification_code(content)
            if code and code not in seen_codes:
                seen_codes.add(code)
                codes.append(code)
        if mark_seen:
            with _sessions_lock:
                session["seen_uids"].add(uid)
    return codes


def wait_for_verification_email(session_id: str, timeout: int = None) -> str | None:
    if timeout is None:
        timeout = EMAIL_WAIT_TIMEOUT

    with _sessions_lock:
        session = _sessions.get(session_id)
    if not session:
        print("❌ 未找到 custom2925 会话")
        return None

    alias_email = session["alias_email"]
    created_at = session["created_at"]

    print(f"⏳ 等待 2925 验证邮件（收件别名: {alias_email}，最长 {timeout}s）...")
    start = time.time()
    while time.time() - start < timeout:
        with _sessions_lock:
            seen_uids = set(session["seen_uids"])

        try:
            messages = _fetch_matching_messages(alias_email, created_at, seen_uids)
        except Exception as e:
            print(f"  查询 2925 邮件失败: {e}")
            messages = []

        if messages:
            for _, msg, _ in messages:
                subject = _decode_header_value(msg.get("Subject"))
                print(f"\n📧 发现 2925/OpenAI 邮件: {subject}")
            codes = _extract_codes_from_messages(session, messages, mark_seen=True)
            if codes:
                return codes[0]

        elapsed = int(time.time() - start)
        print(f"  等待中... ({elapsed}s / {timeout}s)  收件别名: {alias_email}", end="\r")
        time.sleep(EMAIL_POLL_INTERVAL)

    print(f"\n⏰ 等待 2925 验证邮件超时（收件别名: {alias_email}）")
    return None


def list_verification_codes(session_id: str) -> list[str]:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if not session:
        return []

    alias_email = session["alias_email"]
    created_at = session["created_at"]
    with _sessions_lock:
        seen_uids = set(session["seen_uids"])

    try:
        messages = _fetch_matching_messages(alias_email, created_at, seen_uids)
    except Exception as e:
        print(f"  列出 2925 验证码失败: {e}")
        return []

    return _extract_codes_from_messages(session, messages, mark_seen=False)
