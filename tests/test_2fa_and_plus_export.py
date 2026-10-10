import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.two_factor_service import clean_base32_secret, generate_totp_code
from app.stored_accounts import (
    load_accounts_from_file,
    update_account_check_result_in_file,
    update_account_status_in_file,
)
from app.utils import save_to_txt
from app.server import app, state


class Test2FAAndPlusExport(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_totp_generation(self):
        # 32 位 Base32 密钥测试
        secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        code = generate_totp_code(secret)
        self.assertTrue(code.isdigit())
        self.assertEqual(len(code), 6)

        # 包含空格/小写的清洗测试
        cleaned = clean_base32_secret("jbswy3dp ehpk3pxp jbswy3dp ehpk3pxp")
        self.assertEqual(cleaned, secret)

    def test_12_field_persistence_and_compatibility(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            acc_file = Path(tmpdir) / "registered_accounts.txt"

            # 兼容旧格式（11 字段和 8 字段）
            old_content = (
                "old1@test.com|pwd1|2026-10-06 10:00:00|成功|cred1|mailtm|Free|已确认有试用资格|额度说明|到期时间|正常可用\n"
                "old2@test.com|pwd2|2026-10-06 10:01:00|成功|cred2|custom2925|Plus|会员免试用\n"
            )
            acc_file.write_text(old_content, encoding="utf-8")

            # 读取旧格式，验证 two_factor_secret 为空字符串
            records = load_accounts_from_file(str(acc_file))
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["two_factor_secret"], "")
            self.assertEqual(records[1]["two_factor_secret"], "")

            # 使用 save_to_txt 写入包含 2FA 密钥的 12 字段记录
            with patch("app.utils.cfg.files.accounts_file", str(acc_file)):
                save_to_txt(
                    email="new_2fa@test.com",
                    password="Password123!",
                    status="已注册",
                    provider="mailtm",
                    plan="Plus",
                    trial_status="会员免试用",
                    two_factor_secret="MFRGGZDFMY2TMOBWGA======",
                )

            records_after = load_accounts_from_file(str(acc_file))
            self.assertEqual(len(records_after), 3)
            new_rec = [r for r in records_after if r["email"] == "new_2fa@test.com"][0]
            self.assertEqual(new_rec["two_factor_secret"], "MFRGGZDFMY2TMOBWGA======")
            self.assertEqual(new_rec["plan"], "Plus")
            self.assertFalse(new_rec["is_paid"])  # Membership text alone does not verify billing.

            # 验证 update_account_check_result_in_file 保留 2FA 密钥
            update_account_check_result_in_file(
                str(acc_file),
                "new_2fa@test.com",
                plan="Team",
                trial_status="团队会员",
            )
            records_updated = load_accounts_from_file(str(acc_file))
            up_rec = [r for r in records_updated if r["email"] == "new_2fa@test.com"][0]
            self.assertEqual(up_rec["plan"], "Team")
            self.assertEqual(up_rec["two_factor_secret"], "MFRGGZDFMY2TMOBWGA======")

    def test_settings_api_referral_and_2fa(self):
        # 测试 GET /api/settings 返回 referral_url 和 enable_2fa
        resp = self.client.get("/api/settings")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("referral_url", data)
        self.assertIn("enable_2fa", data)

        # 测试 POST /api/settings 保存 referral_url 和 enable_2fa
        post_resp = self.client.post(
            "/api/settings",
            json={
                "referral_url": "https://chatgpt.com/invite/abcdef123456",
                "enable_2fa": True,
            },
        )
        self.assertEqual(post_resp.status_code, 200)
        self.assertEqual(state.referral_url, "https://chatgpt.com/invite/abcdef123456")
        self.assertTrue(state.enable_2fa)

    def test_export_accounts_2fa_format_and_plus_all_filter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            acc_file = Path(tmpdir) / "registered_accounts.txt"
            content = (
                # 账号 1: 拥有试用资格 + 有 2FA
                "trial_user@test.com|TrialPass123|2026-10-06 12:00:00|成功|cred1|mailtm|Free|已确认有试用资格|额度|到期|正常可用|SECRET32CHARACTERSFOR2FATOTPTEST\n"
                # 账号 2: 已是 Plus 会员 + 有 2FA
                "plus_user@test.com|PlusPass456|2026-10-06 12:01:00|成功|cred2|mailtm|Plus|会员免试用|额度|到期|正常可用|ANOTHERSECRET32FOR2FATOTPTEST12\n"
                # 账号 3: 拥有试用资格但未开 2FA
                "no_2fa_trial@test.com|No2faPass789|2026-10-06 12:02:00|成功|cred3|mailtm|Free|已确认有试用资格|额度|到期|正常可用|\n"
                # 账号 4: 纯普通免费账号（无试用、无Plus）
                "free_user@test.com|FreePass999|2026-10-06 12:03:00|成功|cred4|mailtm|Free|无资格|额度|到期|正常可用|\n"
            )
            acc_file.write_text(content, encoding="utf-8")

            with patch("app.server.cfg.files.accounts_file", str(acc_file)):
                # 1. 测试一键筛选 plus_all 并以 2fa 格式导出
                resp = self.client.get("/api/accounts/export?format=2fa&filter=plus_all")
                self.assertEqual(resp.status_code, 200)
                text = resp.data.decode("utf-8")
                lines = [l for l in text.splitlines() if l.strip()]

                # 必须命中 3 个账号（trial_user, plus_user, no_2fa_trial），不应包含 free_user
                self.assertEqual(len(lines), 3)

                # 校验导出格式必须为严格 5 个横杠: 邮箱-----密码-----32位2fa
                self.assertEqual(
                    lines[0],
                    "trial_user@test.com-----TrialPass123-----SECRET32CHARACTERSFOR2FATOTPTEST",
                )
                self.assertEqual(
                    lines[1],
                    "plus_user@test.com-----PlusPass456-----ANOTHERSECRET32FOR2FATOTPTEST12",
                )
                self.assertEqual(
                    lines[2],
                    "no_2fa_trial@test.com-----No2faPass789-----未开启2FA",
                )

                # 2. 测试仅试用筛选 trial
                resp_trial = self.client.get("/api/accounts/export?format=2fa&filter=trial")
                self.assertEqual(resp_trial.status_code, 200)
                trial_lines = [l for l in resp_trial.data.decode("utf-8").splitlines() if l.strip()]
                self.assertEqual(len(trial_lines), 2)

                # 3. 测试仅已支付筛选 paid
                resp_paid = self.client.get("/api/accounts/export?format=2fa&filter=paid")
                self.assertEqual(resp_paid.status_code, 200)
                paid_lines = [l for l in resp_paid.data.decode("utf-8").splitlines() if l.strip()]
                self.assertEqual(len(paid_lines), 1)
                self.assertEqual(
                    paid_lines[0],
                    "plus_user@test.com-----PlusPass456-----ANOTHERSECRET32FOR2FATOTPTEST12",
                )


if __name__ == "__main__":
    unittest.main()
