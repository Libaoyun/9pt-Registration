import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from requests.exceptions import ConnectionError

from app.account_checker import check_account_status, _decode_jwt
from app.stored_accounts import (
    load_accounts_from_file,
    update_account_check_result_in_file,
)
from app.server import app


class AccountCheckerUnitTests(unittest.TestCase):
    def test_decode_jwt_extracts_payload(self):
        # Header: {"alg":"none"} -> eyJhbGciOiJub25lIn0
        # Payload: {"https://api.openai.com/auth":{"plan_type":"plus"}} -> eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsicGxhbl90eXBlIjoicGx1cyJ9fQ
        fake_jwt = "eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsicGxhbl90eXBlIjoicGx1cyJ9fQ."
        decoded = _decode_jwt(fake_jwt)
        self.assertEqual(
            decoded.get("https://api.openai.com/auth", {}).get("plan_type"), "plus"
        )

    @patch("app.account_checker.curl_requests.get", side_effect=ConnectionError("offline"))
    def test_check_account_status_jwt_fallback_free(self, mock_get):
        fake_jwt = "eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsicGxhbl90eXBlIjoiZnJlZSJ9fQ."
        res = check_account_status("test@example.com", access_token=fake_jwt)
        self.assertEqual(res["plan"], "未验证 (Free)")
        self.assertFalse(res["is_paid"])
        self.assertEqual(res["trial_status"], "待官方确认")

    @patch("app.account_checker.curl_requests.get", side_effect=ConnectionError("offline"))
    def test_check_account_status_jwt_fallback_plus(self, mock_get):
        fake_jwt = "eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsicGxhbl90eXBlIjoicGx1cyJ9fQ."
        res = check_account_status("vip@example.com", access_token=fake_jwt)
        self.assertEqual(res["plan"], "未验证 (Plus)")
        self.assertFalse(res["is_paid"])
        self.assertEqual(res["trial_status"], "待官方确认")

    @patch("app.account_checker.curl_requests.get")
    def test_check_account_status_api_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "account_plan": {
                "is_paid_subscription_active": False,
                "subscription_plan": "chatgptfreeplan",
                "account_has_had_paid_subscription": False,
                "was_paid_customer": False,
            }
        }
        mock_get.return_value = mock_resp

        res = check_account_status("newuser@example.com", access_token="token-abc")
        self.assertEqual(res["plan"], "Free")
        self.assertFalse(res["is_paid"])
        self.assertEqual(res["trial_status"], "待官方确认")

    @patch("app.account_checker.curl_requests.get")
    def test_check_account_status_banned(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = '{"error": {"code": "account_deactivated", "message": "Your account has been deactivated."}}'
        mock_get.return_value = mock_resp

        res = check_account_status("banned@example.com", access_token="token-banned")
        self.assertEqual(res["account_status"], "🚫 已封禁/停用")
        self.assertFalse(res["is_paid"])
        self.assertIn("未知", res["quota"])

    @patch("app.account_checker.curl_requests.get")
    def test_check_account_status_token_expired(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = '{"detail": "token_expired"}'
        mock_get.return_value = mock_resp

        res = check_account_status("expired@example.com", access_token="token-expired")
        self.assertIn("Token失效", res["account_status"])

    def test_stored_accounts_loads_and_updates_plan_fields(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
            handle.write(
                "acc1@test.com|pass1|20261006_120000|已注册|mailbox1|mailtm|Free|享有试用资格|动态免费额度|永久免费|🟢 正常可用\n"
            )
            handle.write(
                "acc2@test.com|pass2|20261006_120000|已注册|mailbox2|mailtm\n"
            )
            file_path = handle.name

        records = load_accounts_from_file(file_path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["plan"], "Free")
        self.assertEqual(records[0]["trial_status"], "享有试用资格")
        self.assertEqual(records[0]["timestamp"], "2026-10-06 12:00:00")
        self.assertEqual(records[0]["quota"], "动态免费额度")
        self.assertEqual(records[0]["expires_at"], "永久免费")
        self.assertEqual(records[1]["plan"], "未检测")

        update_account_check_result_in_file(
            file_path,
            "acc2@test.com",
            "Plus",
            "已是会员(无需试用)",
            quota="80次/3h (GPT-4o)",
            expires_at="2026-11-06 15:30",
            account_status="🟢 正常可用"
        )
        updated_records = load_accounts_from_file(file_path)
        self.assertEqual(updated_records[1]["plan"], "Plus")
        self.assertFalse(updated_records[1]["is_paid"])  # A stored plan alone is not payment evidence.
        self.assertEqual(updated_records[1]["trial_status"], "已是会员(无需试用)")
        self.assertEqual(updated_records[1]["quota"], "80次/3h (GPT-4o)")
        self.assertEqual(updated_records[1]["expires_at"], "2026-11-06 15:30")

    def test_server_check_one_endpoint(self):
        client = app.test_client()
        resp = client.post("/api/accounts/check-one", json={"email": "nonexistent@test.com"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["email"], "nonexistent@test.com")
        self.assertIn("plan", data)
        self.assertIn("trial_status", data)
        self.assertIn("quota", data)
        self.assertIn("expires_at", data)
        self.assertIn("account_status", data)
