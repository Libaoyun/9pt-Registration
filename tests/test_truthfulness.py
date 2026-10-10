"""Regressions for false eligibility, false health and mixed credentials."""
import base64
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from requests.exceptions import ConnectionError

from app.account_checker import PROMO_UNCONFIRMED, check_account_status, _decode_jwt
from app.oauth_service import save_codex_tokens
from app.server import app
from app.stored_accounts import is_trial_eligible, load_accounts_from_file, update_account_check_result_in_file
from app.utils import save_to_txt


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.unsigned"


class TruthfulnessTests(unittest.TestCase):
    def setUp(self):
        network = patch("app.account_checker.curl_requests.get", side_effect=ConnectionError("offline"))
        self.get = network.start()
        self.addCleanup(network.stop)
        self.client = app.test_client()

    def response(self, data, status=200, text=""):
        self.get.side_effect = None
        response = MagicMock(status_code=status, text=text)
        response.json.return_value = data
        self.get.return_value = response
        return response

    def test_offline_claims_are_not_paid_or_healthy(self):
        token = jwt({"https://api.openai.com/auth": {"plan_type": "plus"},
                     "exp": time.time() + 3600})
        result = check_account_status("owner@example.com", token)
        self.assertEqual(result["plan"], "未验证 (Plus)")
        self.assertFalse(result["verified"])
        self.assertFalse(result["is_paid"])
        self.assertIn("未在线验证", result["account_status"])
        self.assertEqual(result["trial_status"], "待官方确认")
        self.assertIn("未知", result["quota"])
        self.assertIn("未知", result["expires_at"])

    def test_expired_claims_do_not_become_subscription_expiry(self):
        result = check_account_status("owner@example.com", jwt({"exp": time.time() - 1}))
        self.get.assert_not_called()
        self.assertIn("Token已过期", result["account_status"])
        self.assertIn("未知", result["expires_at"])
        self.assertIn("token_expires_at", result["details"])

    def test_invalid_jwt_payloads_are_not_identity(self):
        for claims in ([], "string", None, 42):
            self.assertEqual(_decode_jwt(jwt(claims)), {})
        result = check_account_status("owner@example.com", "opaque-value")
        self.assertFalse(result["verified"])
        self.assertEqual(result["plan"], "未检测")

    def test_http_200_unknown_schema_is_not_success(self):
        for data in ({}, {"account_plan": {}}, [], {"account_plan": {"subscription_plan": "unknown"}}):
            self.response(data)
            result = check_account_status("owner@example.com", "web-test")
            self.assertFalse(result["verified"])
            self.assertEqual(result["plan"], "未检测")
            self.assertIn("未在线验证", result["account_status"])

    def test_string_false_is_not_active_subscription(self):
        self.response({"account_plan": {"subscription_plan": "plus",
                                       "is_paid_subscription_active": "false"}})
        result = check_account_status("owner@example.com", "web-test")
        self.assertFalse(result["verified"])
        self.assertFalse(result["is_paid"])

    def test_free_account_does_not_prove_trial_eligibility(self):
        self.response({"account_plan": {"subscription_plan": "chatgptfreeplan",
                                       "is_paid_subscription_active": False,
                                       "account_has_had_paid_subscription": False}})
        result = check_account_status("owner@example.com", "web-test")
        self.assertTrue(result["verified"])
        self.assertEqual(result["plan"], "Free")
        self.assertEqual(result["trial_status"], "待官方确认")
        self.assertIn("未知", result["quota"])
        self.assertIn("未知", result["expires_at"])
        self.assertEqual(self.get.call_count, 1)  # No private billing credit request.

    def test_explicit_subscription_expiry_is_kept(self):
        self.response({"account_plan": {"subscription_plan": "chatgptplusplan",
                                       "is_paid_subscription_active": True,
                                       "subscription_expires_at": "2026-11-01T00:00:00Z"}})
        result = check_account_status("owner@example.com", "web-test")
        self.assertTrue(result["is_paid"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["expires_at"], "2026-11-01T00:00:00+00:00")
        self.assertIn("未知", result["quota"])

    def test_modern_accounts_with_plus_promo_detects_trial(self):
        self.response({
            "accounts": {
                "acc-1": {
                    "entitlement": {
                        "subscription_plan": "free",
                        "is_paid_subscription_active": False,
                    },
                    "eligible_promo_campaigns": {
                        "plus": {"id": "plus-1-month-free", "name": "1 Month Trial"}
                    }
                }
            }
        })
        result = check_account_status("owner@example.com", "web-test")
        self.assertTrue(result["verified"])
        self.assertEqual(result["plan"], "Free")
        self.assertEqual(result["trial_status"], "已确认有试用资格 (Plus 1个月试用)")
        self.assertEqual(result["quota"], "1个月Plus免费试用待激活")
        self.assertEqual(result["expires_at"], "无订阅 (标准免费)")
        self.assertEqual(result["account_status"], "🟢 在线已验证")

    def test_promo_without_plus_trial_evidence_is_not_eligible(self):
        """活动列表非空、或关键字里带 free/trial 子串，都不足以断言 Plus 试用资格。"""
        for campaign in (
            {"promofree": {"id": "promofree"}},          # 旧逻辑按子串命中 free，属误报
            {"free_tier_news": {"id": "newsletter"}},    # 与试用无关的活动
            {"plus": {}},                                # 只有 plus，没有免费/试用词元
            {"trial": {"id": "free-trial-of-something"}},  # 有 free/trial，但没有 plus
            {"survey_2026": {"id": "in-product-survey"}},
        ):
            campaign_label = repr(campaign)
            self.response({
                "accounts": {
                    "acc-3": {
                        "entitlement": {
                            "subscription_plan": "free",
                            "is_paid_subscription_active": False,
                        },
                        "eligible_promo_campaigns": campaign,
                    }
                }
            })
            result = check_account_status("owner@example.com", "web-test")
            self.assertTrue(result["verified"], campaign_label)
            self.assertEqual(result["trial_status"], PROMO_UNCONFIRMED, campaign_label)
            self.assertIn("未知", result["quota"], campaign_label)
            self.assertFalse(is_trial_eligible(result["trial_status"]), campaign_label)

    def test_legacy_promo_field_without_plus_trial_evidence_is_not_eligible(self):
        self.response({
            "account_plan": {"subscription_plan": "chatgptfreeplan",
                             "is_paid_subscription_active": False},
            "eligible_promo_campaigns": {"promofree": {"id": "promofree"}},
        })
        result = check_account_status("owner@example.com", "web-test")
        self.assertTrue(result["verified"])
        self.assertEqual(result["trial_status"], PROMO_UNCONFIRMED)
        self.assertIn("未知", result["quota"])
        self.assertFalse(is_trial_eligible(result["trial_status"]))

    def test_modern_accounts_without_promo_is_free(self):
        self.response({
            "accounts": {
                "acc-2": {
                    "entitlement": {
                        "subscription_plan": "free",
                        "is_paid_subscription_active": False,
                    },
                    "eligible_promo_campaigns": {}
                }
            }
        })
        result = check_account_status("owner@example.com", "web-test")
        self.assertTrue(result["verified"])
        self.assertEqual(result["plan"], "Free")
        self.assertEqual(result["trial_status"], "无试用资格 (标准Free)")
        self.assertEqual(result["quota"], "标准免费额度")
        self.assertEqual(result["expires_at"], "无订阅 (标准免费)")

    def test_http_403_is_not_automatically_banned_or_healthy(self):
        self.response({}, status=403, text="Access denied")
        result = check_account_status("owner@example.com", "web-test")
        self.assertFalse(result["verified"])
        self.assertNotIn("封禁", result["account_status"])
        self.assertNotIn("正常", result["account_status"])

    def test_me_plan_does_not_establish_payment(self):
        self.get.side_effect = [
            MagicMock(status_code=404),
            MagicMock(status_code=200, json=lambda: {"account_plan": "plus"}),
        ]
        result = check_account_status("owner@example.com", "web-test")
        self.assertEqual(result["plan"], "Plus")
        self.assertTrue(result["verified"])
        self.assertFalse(result["is_paid"])

    def test_codex_credentials_do_not_query_chatgpt_web(self):
        for token, kind in (("codex-test", "codex"),
                            (jwt({"aud": ["https://api.openai.com/v1"]}), None)):
            self.get.reset_mock()
            result = check_account_status("owner@example.com", token, token_kind=kind)
            self.get.assert_not_called()
            self.assertFalse(result["verified"])
            self.assertEqual(result["details"]["reason"], "different_token_resource")

    def test_saved_codex_record_keeps_resource_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-owner@example.com.json"
            path.write_text(json.dumps({"email": "owner@example.com",
                                        "type": "codex", "access_token": "codex-test"}))
            with patch("app.account_checker.cfg.oauth.token_json_dir", tmp):
                result = check_account_status("owner@example.com")
            self.get.assert_not_called()
            self.assertEqual(result["details"]["reason"], "different_token_resource")

    def test_legacy_and_negative_trial_labels_are_not_eligible(self):
        for status in ("享有试用资格", "没有试用资格", "无资格(曾付费)", "待官方确认", "未检测", ""):
            self.assertFalse(is_trial_eligible(status), status)
        self.assertTrue(is_trial_eligible("已确认有试用资格"))

    def test_new_records_have_no_invented_entitlements(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.txt"
            with patch("app.utils.cfg.files.accounts_file", str(path)):
                save_to_txt("owner@example.com", "test-only", "已注册")
            record = load_accounts_from_file(str(path))[0]
            self.assertEqual(record["plan"], "未检测")
            self.assertEqual(record["trial_status"], "待官方确认")
            self.assertIn("未知", record["quota"])
            self.assertIn("未知", record["expires_at"])
            self.assertIn("未在线验证", record["account_status"])

    def test_failed_persistence_is_not_silently_successful(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.utils.cfg.files.accounts_file", str(Path(tmp) / "accounts.txt")):
                with patch("builtins.open", side_effect=PermissionError("read-only")):
                    with self.assertRaises(PermissionError):
                        save_to_txt("owner@example.com", "test-only")

    def test_sync_refuses_to_mix_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.server.PROJECT_ROOT", Path(tmp) / "registration"):
                response = self.client.post("/api/chat2api/sync")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.get_json()["added_count"], 0)
            self.assertFalse((Path(tmp) / "chat2api" / "data").exists())

    def test_saving_codex_token_does_not_inject_gateway_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "chat2api" / "data" / "token.txt"
            pool.parent.mkdir(parents=True)
            pool.write_text("existing-web-credential\n")
            oauth = types.SimpleNamespace(ak_file=str(root / "export" / "ak.txt"),
                                          rk_file=str(root / "export" / "rk.txt"),
                                          token_json_dir=str(root / "tokens"))
            cpa = types.SimpleNamespace(upload_api_url="", upload_api_token="")
            with patch("app.oauth_service.PROJECT_ROOT", root / "registration"):
                with patch("app.oauth_service._upload_to_cliproxy"):
                    token_file = save_codex_tokens("owner@example.com",
                                                  {"access_token": "codex-test"},
                                                  oauth_cfg=oauth, cpa_cfg=cpa)
            self.assertTrue(Path(token_file).exists())
            self.assertEqual(pool.read_text(), "existing-web-credential\n")

    def test_export_does_not_count_legacy_guesses_or_negative_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.txt"
            path.write_text(
                "old@example.com|p|date|已注册||mailtm|Free|享有试用资格\n"
                "no@example.com|p|date|已注册||mailtm|Free|没有试用资格\n"
                "yes@example.com|p|date|已注册||mailtm|Free|已确认有试用资格\n",
                encoding="utf-8")
            with patch("app.server.cfg.files.accounts_file", str(path)):
                response = self.client.get("/api/accounts/export?format=2fa&filter=trial")
                self.assertEqual(response.status_code, 200)
                text = response.get_data(as_text=True)
                self.assertIn("yes@example.com", text)
                self.assertNotIn("old@example.com", text)
                self.assertNotIn("no@example.com", text)

    def test_saved_plan_requires_payment_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.txt"
            path.write_text("owner@example.com|p|date|已注册||mailtm|Plus|待官方确认\n", encoding="utf-8")
            self.assertFalse(load_accounts_from_file(str(path))[0]["is_paid"])
            evidence = {"verified": True, "is_paid": True,
                        "source": "api_check", "checked_at": "2026-10-07T12:00:00+08:00"}
            update_account_check_result_in_file(str(path), "owner@example.com", "Plus",
                                                "待官方确认", account_status="🟢 在线已验证",
                                                evidence=evidence)
            record = load_accounts_from_file(str(path))[0]
            self.assertTrue(record["is_paid"])
            self.assertTrue(record["verified"])
            self.assertEqual(record["profile_evidence"], evidence)

    def test_invalid_token_type_never_sends_credentials(self):
        result = check_account_status("owner@example.com", access_token=123)
        self.assertFalse(result["verified"])
        self.get.assert_not_called()


class ProtocolRegistrationEligibilityTests(unittest.TestCase):
    """协议版注册阶段同样不得用本地配置（例如邀请链接）推断试用资格。"""

    def _register_with_referral(self, referral_url):
        import app.protocol_register as protocol

        saved = []
        client = MagicMock()
        client.get_csrf.return_value = "csrf-token"
        client.signin_init.return_value = "https://auth.openai.com/authorize"
        client.register_user.return_value = {"_status": 200, "continue_url": ""}
        client.validate_otp.return_value = {"_status": 200}
        client.create_profile.return_value = {"callback_url": "/"}
        client.session_token = "session-token"
        client.get_web_access_token.return_value = "web-access-token"
        # 协议引擎改用真实证据采集层：这里注入"官方返回但未取得任何权益证据"的状态
        client.collect_state.return_value = {
            "email": "unit@example.com",
            "plan": "未检测",
            "plan_raw": "",
            "plan_source": "",
            "is_plus": None,
            "is_paid": None,
            "trial_eligible": None,
            "trial_status": "待官方确认",
            "quota": "未知，请在官方页面确认",
            "expires_at": "未知，未取得订阅到期时间",
            "account_status": "⚪ 未在线验证",
            "verified": False,
            "sources": [],
            "evidence": {},
            "checked_at": "2026-10-07T12:00:00+08:00",
        }

        def fake_save(email, password, status, **kwargs):
            saved.append({"status": status, **kwargs})

        patches = [
            patch.object(protocol.email_providers, "get_provider_info",
                         return_value={"name": "mail.tm"}),
            patch.object(protocol.email_providers, "create_temp_email",
                         return_value=("unit@example.com", "inbox-token", "mail-credential")),
            patch.object(protocol.email_providers, "list_verification_codes",
                         return_value=[]),
            patch.object(protocol.email_providers, "wait_for_verification_email",
                         return_value="123456"),
            patch.object(protocol, "ChatGPTProtocolRegister", return_value=client),
            patch.object(protocol, "save_to_txt", side_effect=fake_save),
            patch.object(protocol.cfg.oauth, "enabled", False),
            patch.object(protocol.time, "sleep", return_value=None),
            patch("app.stored_accounts.get_registered_account_by_email", return_value=None),
        ]
        for entry in patches:
            entry.start()
            self.addCleanup(entry.stop)

        protocol.register_one_account(email_provider="mailtm",
                                      referral_url=referral_url, enable_2fa=False)
        final = [record for record in saved if record["status"] != "邮箱已创建"]
        self.assertTrue(final, "协议注册流程未写回账号记录")
        return final[-1]

    def test_referral_url_does_not_assert_trial_eligibility(self):
        record = self._register_with_referral("https://chatgpt.com/invite/example")
        self.assertEqual(record["trial_status"], "待官方确认")
        self.assertIn("未知", record["quota"])
        self.assertIn("未知", record["expires_at"])
        self.assertFalse(is_trial_eligible(record["trial_status"]))
        self.assertTrue(record["evidence"]["referral_url_configured"])
        self.assertFalse(record["evidence"]["referral_binding_verified"])
