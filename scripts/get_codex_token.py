"""
为已保存的账号单独补取 Codex token JSON。

示例:
    uv run python get_codex_token.py --email user@example.com
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import cfg  # noqa: E402
from app.oauth_service import perform_codex_oauth_login, save_codex_tokens  # noqa: E402
from app.stored_accounts import OAUTH_SUCCESS_STATUS  # noqa: E402
from app.stored_accounts import is_oauth_success_status  # noqa: E402
from app.stored_accounts import load_account_from_file  # noqa: E402
from app.stored_accounts import update_account_status_in_file  # noqa: E402
from app.token_batch_service import _login_existing_mailbox  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(description="为已保存账号补取 Codex token JSON")
    parser.add_argument("--email", required=True, help="registered_accounts.txt 中已有的邮箱")
    parser.add_argument(
        "--accounts-file",
        default=cfg.files.accounts_file,
        help="账号文件路径，默认读取 config.yaml 中的 files.accounts_file",
    )
    return parser


def main():
    args = build_parser().parse_args()
    record = load_account_from_file(args.accounts_file, args.email)

    if is_oauth_success_status(record["status"]):
        print(f"⏭️ 已跳过: {record['email']} 当前状态已是 {OAUTH_SUCCESS_STATUS}")
        return

    provider = record["provider"].strip().lower()

    mailbox_credential = record["mailbox_credential"]
    if not mailbox_credential:
        raise SystemExit("账号记录中缺少邮箱收件凭证，无法重新登录收件箱")

    print(f"📬 正在重新登录 {provider} 收件箱: {record['email']}")
    mail_token = _login_existing_mailbox(provider, record["email"], mailbox_credential)

    print("🔑 正在执行 Codex OAuth 登录...")
    tokens = perform_codex_oauth_login(
        email=record["email"],
        password=record["password"],
        email_provider=provider,
        mail_token=mail_token,
        proxy=None,
    )

    token_path = save_codex_tokens(
        email=record["email"],
        tokens=tokens,
        proxy=None,
    )
    update_account_status_in_file(args.accounts_file, record["email"], OAUTH_SUCCESS_STATUS)
    print(f"✅ Token JSON 已生成: {token_path}")


if __name__ == "__main__":
    main()
