import os
import tempfile
import unittest

from app.config import cfg
from app.mailtm_service import login_existing_email
from app.stored_accounts import (
    OAUTH_SUCCESS_STATUS,
    load_account_from_file,
    load_accounts_from_file,
)
from app.token_batch_service import process_accounts_from_file


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return self.response


class StoredAccountsTests(unittest.TestCase):
    def test_load_account_from_file_reads_mailtm_credentials(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
            handle.write(
                "user1@example.com|chatgpt-pass|20260309_010101|已注册|mailbox-pass|mailtm\n"
            )
            handle.write(
                "user2@example.com|chatgpt-pass-2|20260309_020202|已注册|other-mailbox|temporam\n"
            )
            path = handle.name

        record = load_account_from_file(path, "user1@example.com")

        self.assertEqual(record["email"], "user1@example.com")
        self.assertEqual(record["password"], "chatgpt-pass")
        self.assertEqual(record["mailbox_credential"], "mailbox-pass")
        self.assertEqual(record["provider"], "mailtm")

    def test_load_accounts_from_file_returns_all_records(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
            handle.write(
                "user1@example.com|chatgpt-pass|20260309_010101|已注册|mailbox-pass|mailtm\n"
            )
            handle.write(
                "user2@example.com|chatgpt-pass-2|20260309_020202|已注册|other-mailbox|temporam\n"
            )
            path = handle.name

        records = load_accounts_from_file(path)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["email"], "user1@example.com")
        self.assertEqual(records[1]["provider"], "temporam")


class MailTmLoginTests(unittest.TestCase):
    def test_login_existing_email_returns_token(self):
        session = FakeSession(
            FakeResponse(
                200,
                payload={"token": "jwt-token-123"},
            )
        )

        token = login_existing_email(
            "user@example.com",
            "mailbox-pass",
            session=session,
        )

        self.assertEqual(token, "jwt-token-123")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            session.calls[0]["json"],
            {"address": "user@example.com", "password": "mailbox-pass"},
        )


class TokenBatchServiceTests(unittest.TestCase):
    def test_process_accounts_from_file_reports_realtime_progress(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            accounts_path = os.path.join(tmpdir, "accounts.txt")
            output_dir = os.path.join(tmpdir, "token-output")
            with open(accounts_path, "w", encoding="utf-8") as handle:
                handle.write(
                    f"user1@example.com|chatgpt-pass|20260309_010101|{OAUTH_SUCCESS_STATUS}|mailbox-pass|mailtm\n"
                )
                handle.write(
                    "user2@example.com|chatgpt-pass-2|20260309_020202|已注册|mailbox-pass-2|mailtm\n"
                )

            progress_events = []

            def fake_mail_login(provider, email, mailbox_password):
                return "mail-token"

            def fake_oauth(email, password, email_provider, mail_token, proxy):
                return {"access_token": "a", "refresh_token": "r"}

            def fake_save(email, tokens, oauth_cfg, proxy):
                return os.path.join(oauth_cfg.token_json_dir, f"{email}.json")

            process_accounts_from_file(
                accounts_file=accounts_path,
                output_dir=output_dir,
                proxy=None,
                progress_callback=progress_events.append,
                mail_login_func=fake_mail_login,
                oauth_login_func=fake_oauth,
                save_tokens_func=fake_save,
            )

            self.assertGreaterEqual(len(progress_events), 3)
            self.assertEqual(progress_events[0]["status"], "starting")
            self.assertEqual(progress_events[1]["status"], "skipped_existing_success")
            self.assertEqual(progress_events[1]["completed"], 1)
            self.assertEqual(progress_events[1]["remaining"], 1)
            self.assertEqual(progress_events[-1]["status"], "success")
            self.assertEqual(progress_events[-1]["completed"], 2)
            self.assertEqual(progress_events[-1]["remaining"], 0)

    def test_process_accounts_from_file_skips_accounts_already_marked_oauth_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            accounts_path = os.path.join(tmpdir, "accounts.txt")
            output_dir = os.path.join(tmpdir, "token-output")
            with open(accounts_path, "w", encoding="utf-8") as handle:
                handle.write(
                    f"user1@example.com|chatgpt-pass|20260309_010101|{OAUTH_SUCCESS_STATUS}|mailbox-pass|mailtm\n"
                )
                handle.write(
                    "user2@example.com|chatgpt-pass-2|20260309_020202|已注册|mailbox-pass-2|mailtm\n"
                )

            seen = {"mail_login": [], "oauth": [], "save": []}

            def fake_mail_login(provider, email, mailbox_password):
                seen["mail_login"].append((provider, email, mailbox_password))
                return "mail-token"

            def fake_oauth(email, password, email_provider, mail_token, proxy):
                seen["oauth"].append((email, password, email_provider, mail_token, proxy))
                return {"access_token": "a", "refresh_token": "r"}

            def fake_save(email, tokens, oauth_cfg, proxy):
                seen["save"].append(email)
                return os.path.join(oauth_cfg.token_json_dir, f"{email}.json")

            result = process_accounts_from_file(
                accounts_file=accounts_path,
                output_dir=output_dir,
                proxy=None,
                mail_login_func=fake_mail_login,
                oauth_login_func=fake_oauth,
                save_tokens_func=fake_save,
            )

            self.assertEqual(result["total"], 2)
            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["success"], 1)
            self.assertEqual(result["fail"], 0)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(seen["mail_login"], [("mailtm", "user2@example.com", "mailbox-pass-2")])
            self.assertEqual(seen["oauth"][0][0], "user2@example.com")
            self.assertEqual(seen["save"], ["user2@example.com"])

    def test_process_accounts_from_file_uses_output_directory_for_token_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            accounts_path = os.path.join(tmpdir, "accounts.txt")
            output_dir = os.path.join(tmpdir, "token-output")
            with open(accounts_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "user1@example.com|chatgpt-pass|20260309_010101|已注册|mailbox-pass|mailtm\n"
                )
                handle.write(
                    "user2@example.com|chatgpt-pass-2|20260309_020202|已注册|main-box@2925.com|custom2925\n"
                )

            seen = {"mail_login": [], "oauth": [], "save": []}

            def fake_mail_login(provider, email, mailbox_password):
                seen["mail_login"].append((provider, email, mailbox_password))
                return f"mail-token-{provider}"

            def fake_oauth(email, password, email_provider, mail_token, proxy):
                seen["oauth"].append((email, password, email_provider, mail_token, proxy))
                return {"access_token": "a", "refresh_token": "r"}

            def fake_save(email, tokens, oauth_cfg, proxy):
                seen["save"].append(
                    {
                        "email": email,
                        "tokens": tokens,
                        "ak_file": oauth_cfg.ak_file,
                        "rk_file": oauth_cfg.rk_file,
                        "token_json_dir": oauth_cfg.token_json_dir,
                        "proxy": proxy,
                    }
                )
                return os.path.join(oauth_cfg.token_json_dir, f"{email}.json")

            result = process_accounts_from_file(
                accounts_file=accounts_path,
                output_dir=output_dir,
                proxy=None,
                mail_login_func=fake_mail_login,
                oauth_login_func=fake_oauth,
                save_tokens_func=fake_save,
            )

            self.assertEqual(result["total"], 2)
            self.assertEqual(result["processed"], 2)
            self.assertEqual(result["success"], 2)
            self.assertEqual(result["fail"], 0)
            self.assertEqual(
                seen["mail_login"],
                [
                    ("mailtm", "user1@example.com", "mailbox-pass"),
                    ("custom2925", "user2@example.com", "main-box@2925.com"),
                ],
            )
            self.assertEqual(
                seen["oauth"],
                [
                    ("user1@example.com", "chatgpt-pass", "mailtm", "mail-token-mailtm", None),
                    (
                        "user2@example.com",
                        "chatgpt-pass-2",
                        "custom2925",
                        "mail-token-custom2925",
                        None,
                    ),
                ],
            )
            self.assertEqual(seen["save"][0]["ak_file"], cfg.oauth.ak_file)
            self.assertEqual(seen["save"][0]["rk_file"], cfg.oauth.rk_file)
            self.assertEqual(seen["save"][0]["token_json_dir"], output_dir)

            updated_record = load_account_from_file(accounts_path, "user1@example.com")
            self.assertEqual(updated_record["status"], OAUTH_SUCCESS_STATUS)
            updated_record_2 = load_account_from_file(accounts_path, "user2@example.com")
            self.assertEqual(updated_record_2["status"], OAUTH_SUCCESS_STATUS)


if __name__ == "__main__":
    unittest.main()
