import unittest
import os
import sys
import tempfile
from pathlib import Path

# Add project root and app to sys.path
TEST_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TEST_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.sentinel import Sentinel
from app.two_factor_service import generate_totp_code, clean_base32_secret
from app.server import app, state
from app.utils import save_to_txt
from app.stored_accounts import load_accounts_from_file


class TestProtocolEngine(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_sentinel_pow_structure(self):
        """测试 Sentinel PoW 令牌结构生成与哈希解算"""
        sentinel = Sentinel("test-device-id-1234")
        req_token = sentinel._requirements_token()
        self.assertTrue(req_token.startswith("gAAAAAC"))
        
        pow_token = sentinel._pow_token("seed_12345", "0")
        self.assertTrue(pow_token.startswith("gAAAAAB"))

    def test_totp_generation(self):
        """测试 TOTP 32位密钥清洗与6位双重验证码计算"""
        raw_secret = "JBSWY3DPEHPK3PXP JBSWY3DPEHPK3PXP"
        cleaned = clean_base32_secret(raw_secret)
        self.assertEqual(len(cleaned), 32)
        
        # 验证 TOTP 码为 6 位纯数字
        code = generate_totp_code(cleaned)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_server_status_api(self):
        """测试 /api/status 接口返回结构"""
        res = self.client.get('/api/status')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("is_running", data)
        self.assertIn("current_action", data)
        self.assertIn("logs", data)

    def test_server_settings_api(self):
        """测试 /api/settings GET 和 POST"""
        res = self.client.get('/api/settings')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("parallel", data)
        self.assertIn("enable_2fa", data)

        # POST 更新设置
        post_res = self.client.post('/api/settings', json={
            "parallel": 5,
            "enable_2fa": True,
            "referral_url": "https://chatgpt.com/invite/test1234",
            "proxy": {"enabled": False, "type": "http", "host": "", "port": 8080}
        })
        self.assertEqual(post_res.status_code, 200)
        updated = post_res.get_json()
        self.assertEqual(updated["parallel"], 5)
        self.assertEqual(updated["referral_url"], "https://chatgpt.com/invite/test1234")

    def test_server_providers_api(self):
        """测试 /api/providers 获取与设置"""
        res = self.client.get('/api/providers')
        self.assertEqual(res.status_code, 200)
        providers = res.get_json()
        self.assertIsInstance(providers, list)
        self.assertGreater(len(providers), 0)
        
        # 检查是否包含核心提供商
        provider_ids = [p["id"] for p in providers]
        self.assertIn("mailtm", provider_ids)

    def test_export_5_hyphen_2fa_format(self):
        """测试导出格式: 邮箱-----密码-----32位2fa"""
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False, suffix=".txt") as tf:
            temp_acc_file = tf.name

        try:
            # 写入一条带 2FA 密钥与试用资格的账号记录
            from app.config import cfg
            old_file = cfg.files.accounts_file
            cfg.files.accounts_file = temp_acc_file

            save_to_txt(
                email="test_protocol@example.com",
                password="TestPassword123!",
                status="已注册",
                mailtm_password="token123",
                provider="mailtm",
                plan="Free",
                trial_status="享有试用资格",
                quota="动态免费额度",
                expires_at="永久免费",
                account_status="🟢 正常可用",
                two_factor_secret="JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
            )

            res = self.client.get('/api/accounts/export?format=2fa&filter=all')
            self.assertEqual(res.status_code, 200)
            text = res.get_data(as_text=True)
            self.assertIn("test_protocol@example.com-----TestPassword123!-----JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP", text)

            # 测试 CSV 导出
            res_csv = self.client.get('/api/accounts/export?format=csv&filter=all')
            self.assertEqual(res_csv.status_code, 200)
            csv_text = res_csv.get_data(as_text=True)
            self.assertIn("test_protocol@example.com", csv_text)

            # 测试 combo 导出
            res_combo = self.client.get('/api/accounts/export?format=combo&filter=all')
            self.assertEqual(res_combo.status_code, 200)
            combo_text = res_combo.get_data(as_text=True)
            self.assertIn("test_protocol@example.com----TestPassword123!", combo_text)

            cfg.files.accounts_file = old_file
        finally:
            if os.path.exists(temp_acc_file):
                os.remove(temp_acc_file)

    def test_import_accounts_api(self):
        """测试 /api/accounts/import 批量导入与跳过逻辑"""
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False, suffix=".txt") as tf:
            temp_acc_file = tf.name
            tf.write("existing@proto.com|OldPass123|2026-10-06 12:00:00|已注册|cred|mailtm|Free|待官方确认|动态免费额度|永久免费|🟢 在线已验证|MFASECRET\n")

        try:
            from app.config import cfg
            old_file = cfg.files.accounts_file
            cfg.files.accounts_file = temp_acc_file

            import_text = (
                "existing@proto.com-----NewPass-----NEWSECRET\n"
                "imported1@proto.com-----PassOne123!-----MFASECRET32CHARS\n"
                "imported2@proto.com----PassTwo456\n"
            )
            resp = self.client.post("/api/accounts/import", json={"text": import_text})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get("success"))
            self.assertEqual(data.get("imported"), 2)
            self.assertEqual(data.get("skipped"), 1)

            # 验证在表格中能完整加载展示
            resp_acc = self.client.get("/api/accounts")
            self.assertEqual(resp_acc.status_code, 200)
            accounts = resp_acc.get_json()
            emails = [a["email"] for a in accounts]
            self.assertIn("existing@proto.com", emails)
            self.assertIn("imported1@proto.com", emails)
            self.assertIn("imported2@proto.com", emails)

            cfg.files.accounts_file = old_file
        finally:
            if os.path.exists(temp_acc_file):
                os.remove(temp_acc_file)

    def test_already_registered_helpers(self):
        from app.stored_accounts import is_registered_status, get_registered_account_by_email
        self.assertTrue(is_registered_status("已注册"))
        self.assertTrue(is_registered_status("已注册/OAuth成功"))
        self.assertTrue(is_registered_status("已在官方注册(跳过)"))
        self.assertTrue(is_registered_status("导入账号"))
        self.assertFalse(is_registered_status("邮箱已创建"))
        self.assertFalse(is_registered_status("失败: 验证码错误"))

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False, suffix=".txt") as tf:
            temp_acc_file = tf.name
            tf.write("user@test.com|P@ss123|2026-10-07|已注册|c|m|Free|待确认|q|e|h|2fa\n")

        try:
            rec = get_registered_account_by_email(temp_acc_file, "user@test.com")
            self.assertIsNotNone(rec)
            self.assertEqual(rec["password"], "P@ss123")
            self.assertIsNone(get_registered_account_by_email(temp_acc_file, "nonexistent@test.com"))
        finally:
            if os.path.exists(temp_acc_file):
                os.remove(temp_acc_file)


if __name__ == "__main__":
    unittest.main()

