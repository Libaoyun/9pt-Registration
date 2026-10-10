import unittest
from unittest.mock import patch, MagicMock
from app.server import app
import tempfile
from pathlib import Path


class TestUnifiedFeatures(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    @patch("requests.get")
    def test_proxy_test_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success",
            "query": "104.28.19.1",
            "country": "United States",
            "city": "Los Angeles",
            "isp": "Cloudflare, Inc."
        }
        mock_get.return_value = mock_resp

        payload = {
            "proxy": {
                "type": "socks5",
                "host": "127.0.0.1",
                "port": 7890
            }
        }
        resp = self.client.post("/api/proxy/test", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("ip"), "104.28.19.1")
        self.assertEqual(data.get("query"), "104.28.19.1")
        self.assertEqual(data.get("country"), "United States")

    def test_proxy_test_missing_params(self):
        resp = self.client.post("/api/proxy/test", json={"proxy": {"host": ""}})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data.get("success"))

    def test_export_accounts_csv_and_combo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            acc_file = Path(tmpdir) / "registered_accounts.txt"
            # Format: email|password|timestamp|status|mailbox_credential|provider|plan|trial_status
            sample_content = (
                "user1@test.com|pass123|2026-10-06 12:00:00|成功|cred1|mailtm|Free|已确认有试用资格\n"
                "user2@test.com|pass456|2026-10-06 12:01:00|成功|cred2|custom2925|Plus|会员免试用\n"
            )
            acc_file.write_text(sample_content, encoding="utf-8")

            with patch("app.server.cfg.files.accounts_file", str(acc_file)):
                # Test CSV export
                resp = self.client.get("/api/accounts/export?format=csv")
                self.assertEqual(resp.status_code, 200)
                self.assertIn("text/csv", resp.headers.get("Content-Type", ""))
                text = resp.data.decode("utf-8")
                self.assertIn("ChatGPT邮箱", text)
                self.assertIn("user1@test.com", text)
                self.assertIn("user2@test.com", text)

                # Test combo format with trial filter
                resp_trial = self.client.get("/api/accounts/export?format=combo&filter=trial")
                self.assertEqual(resp_trial.status_code, 200)
                trial_text = resp_trial.data.decode("utf-8")
                self.assertIn("user1@test.com----pass123----Free----已确认有试用资格", trial_text)
                self.assertNotIn("user2@test.com", trial_text)

                # Test combo format with paid filter
                resp_paid = self.client.get("/api/accounts/export?format=combo&filter=paid")
                self.assertEqual(resp_paid.status_code, 200)
                paid_text = resp_paid.data.decode("utf-8")
                self.assertIn("user2@test.com", paid_text)
                self.assertNotIn("user1@test.com", paid_text)

    @patch("app.server._ensure_chat2api_running", return_value=True)
    def test_chat2api_start(self, mock_ensure):
        resp = self.client.post("/api/chat2api/start")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get("status"), "running")

    def test_chat2api_stop(self):
        resp = self.client.post("/api/chat2api/stop")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get("status"), "stopped")

    @patch("app.server._ensure_chat2api_running", return_value=True)
    @patch("requests.request")
    def test_v1_reverse_proxy_forwarding(self, mock_req, mock_ensure):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_content.return_value = [b'{"id":"chatcmpl-test","choices":[]}']
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_req.return_value = mock_resp

        resp = self.client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": []})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("chatcmpl-test", resp.data.decode("utf-8"))

    def test_import_accounts_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            acc_file = Path(tmpdir) / "registered_accounts.txt"
            initial_content = "existing@test.com|OldPass123|2026-10-06 12:00:00|已注册|cred|mailtm|Free|待官方确认|动态免费额度|永久免费|🟢 在线已验证|BASE32SECRET\n"
            acc_file.write_text(initial_content, encoding="utf-8")

            with patch("app.server.cfg.files.accounts_file", str(acc_file)):
                import_text = (
                    "existing@test.com-----NewPass-----NEWSECRET\n"
                    "imported1@test.com-----PassOne123!-----MFASECRET32CHARS\n"
                    "imported2@test.com----PassTwo456\n"
                )
                resp = self.client.post("/api/accounts/import", json={"text": import_text})
                self.assertEqual(resp.status_code, 200)
                data = resp.get_json()
                self.assertTrue(data.get("success"))
                self.assertEqual(data.get("imported"), 2)
                self.assertEqual(data.get("skipped"), 1)

                # 检查在表格 API 中依然能完整加载展示
                resp_acc = self.client.get("/api/accounts")
                self.assertEqual(resp_acc.status_code, 200)
                accounts = resp_acc.get_json()
                emails = [a["email"] for a in accounts]
                self.assertIn("existing@test.com", emails)
                self.assertIn("imported1@test.com", emails)
                self.assertIn("imported2@test.com", emails)

    def test_already_registered_helpers(self):
        from app.stored_accounts import is_registered_status, get_registered_account_by_email
        self.assertTrue(is_registered_status("已注册"))
        self.assertTrue(is_registered_status("已注册/OAuth成功"))
        self.assertTrue(is_registered_status("已在官方注册(跳过)"))
        self.assertTrue(is_registered_status("导入账号"))
        self.assertFalse(is_registered_status("邮箱已创建"))
        self.assertFalse(is_registered_status("失败: 验证码错误"))
        self.assertFalse(is_registered_status(""))

        with tempfile.TemporaryDirectory() as tmpdir:
            acc_file = Path(tmpdir) / "registered_accounts.txt"
            acc_file.write_text("user@test.com|P@ss123|2026-10-07|已注册|c|m|Free|待确认|q|e|h|2fa\n", encoding="utf-8")
            rec = get_registered_account_by_email(str(acc_file), "user@test.com")
            self.assertIsNotNone(rec)
            self.assertEqual(rec["password"], "P@ss123")
            self.assertIsNone(get_registered_account_by_email(str(acc_file), "nonexistent@test.com"))


if __name__ == "__main__":
    unittest.main()

