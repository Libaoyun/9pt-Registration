"""真实状态采集器的回归测试：证据优先、无证据即未知、接口被拦截如实记录。"""
import json
import unittest

from app.account_probe import (
    STATUS_BLOCKED,
    STATUS_TOKEN_INVALID,
    STATUS_VERIFIED,
    UNKNOWN_EXPIRY,
    UNKNOWN_PLAN,
    UNKNOWN_QUOTA,
    UNKNOWN_TRIAL,
    build_otpauth_url,
    derive_state,
    identify_plus_trial,
    new_state,
    normalize_plan,
    probe_in_browser,
    probe_with_token,
)


class FakeResponse:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text if payload is None else json.dumps(payload)


class FakeSession:
    """最小可用的 duck-typing HTTP 会话，用于离线回归。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        for key, response in self.routes.items():
            if key in url:
                return response
        return FakeResponse(status_code=404, text="{}")

    def close(self):
        pass


class DeriveStateTests(unittest.TestCase):
    def test_plan_normalization(self):
        self.assertEqual(normalize_plan("chatgptplusplan"), "Plus")
        self.assertEqual(normalize_plan("chatgptfreeplan"), "Free")
        self.assertEqual(normalize_plan("chatgptproplan"), "Pro")
        self.assertIsNone(normalize_plan("something-unknown"))

    def test_no_payload_keeps_everything_unknown(self):
        state = derive_state("owner@example.com", {}, [{"name": "me", "status": 0}])
        self.assertEqual(state["plan"], UNKNOWN_PLAN)
        self.assertEqual(state["trial_status"], UNKNOWN_TRIAL)
        self.assertEqual(state["quota"], UNKNOWN_QUOTA)
        self.assertEqual(state["expires_at"], UNKNOWN_EXPIRY)
        self.assertFalse(state["verified"])
        self.assertIsNone(state["is_plus"])
        self.assertIsNone(state["trial_eligible"])

    def test_plus_trial_campaign_is_confirmed(self):
        payload = {
            "accounts_check": json.dumps({
                "accounts": {
                    "acc-1": {
                        "entitlement": {
                            "subscription_plan": "free",
                            "is_paid_subscription_active": False,
                        },
                        "eligible_promo_campaigns": {"plus-1-month-free": {"id": "plus-1-month-free"}},
                    }
                }
            })
        }
        state = derive_state("owner@example.com", payload, [{"name": "accounts_check", "status": 200}])
        self.assertTrue(state["verified"])
        self.assertEqual(state["plan"], "Free")
        self.assertIs(state["trial_eligible"], True)
        self.assertIn("已确认有试用资格", state["trial_status"])
        self.assertIs(state["is_plus"], False)
        self.assertEqual(state["account_status"], STATUS_VERIFIED)

    def test_active_plus_subscription_is_plus(self):
        payload = {
            "accounts_check": json.dumps({
                "accounts": {
                    "acc-2": {
                        "entitlement": {
                            "subscription_plan": "chatgptplusplan",
                            "is_paid_subscription_active": True,
                            "subscription_expires_at": "2026-12-01T00:00:00Z",
                        }
                    }
                }
            })
        }
        state = derive_state("plus@example.com", payload, [{"name": "accounts_check", "status": 200}])
        self.assertEqual(state["plan"], "Plus")
        self.assertIs(state["is_plus"], True)
        self.assertIs(state["is_paid"], True)
        self.assertEqual(state["expires_at"], "2026-12-01 08:00:00")
        self.assertEqual(state["trial_status"], "👑 已激活Plus会员")

    def test_promo_without_plus_evidence_stays_unknown(self):
        payload = {
            "accounts_check": json.dumps({
                "accounts": {
                    "acc-3": {
                        "entitlement": {"subscription_plan": "free", "is_paid_subscription_active": False},
                        "eligible_promo_campaigns": {"promofree": {"id": "newsletter"}},
                    }
                }
            })
        }
        state = derive_state("owner@example.com", payload, [{"name": "accounts_check", "status": 200}])
        self.assertIsNone(state["trial_eligible"])
        self.assertIn("未确认为Plus试用", state["trial_status"])

    def test_explicit_trial_boolean_is_used_when_present(self):
        payload = json.dumps({"entitlement": {"has_plus_trial_eligibility": True}})
        eligible, note, evidence = identify_plus_trial({"me": payload})
        self.assertTrue(eligible)
        self.assertIn("已确认有试用资格", note)
        self.assertTrue(evidence["trial_boolean_fields"])

    def test_unauthorized_and_blocked_are_distinguished(self):
        unauthorized = derive_state("a@example.com", {}, [{"name": "me", "status": 401}])
        self.assertEqual(unauthorized["account_status"], STATUS_TOKEN_INVALID)
        blocked = derive_state("a@example.com", {}, [{"name": "me", "status": 403}])
        self.assertEqual(blocked["account_status"], STATUS_BLOCKED)
        deactivated = derive_state("a@example.com", {}, [{"name": "me", "status": 403, "note": "deactivated"}])
        self.assertIn("封禁", deactivated["account_status"])

    def test_otpauth_url_is_standard(self):
        url = build_otpauth_url("owner@example.com", "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")
        self.assertTrue(url.startswith("otpauth://totp/OpenAI:"))
        self.assertIn("secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP", url)

    def test_new_state_has_no_invented_values(self):
        state = new_state("x@example.com")
        self.assertEqual(state["plan"], UNKNOWN_PLAN)
        self.assertEqual(state["sources"], [])
        self.assertEqual(state["evidence"], {})


class ChannelTests(unittest.TestCase):
    def test_probe_with_token_records_each_attempt(self):
        session = FakeSession({
            "/backend-api/me": FakeResponse(401, '{"detail":"Unauthorized"}'),
        })
        state = probe_with_token("web-token", email="owner@example.com", session=session)
        self.assertFalse(state["verified"])
        self.assertEqual(state["account_status"], STATUS_TOKEN_INVALID)
        self.assertEqual(state["sources"][0]["name"], "me")

    def test_probe_with_token_parses_accounts_check(self):
        session = FakeSession({
            "/backend-api/me": FakeResponse(200, json.dumps({"account_plan": "chatgptplusplan"})),
            "/backend-api/accounts/check/v4-2023-04-27": FakeResponse(200, json.dumps({
                "accounts": {
                    "acc": {
                        "entitlement": {
                            "subscription_plan": "chatgptplusplan",
                            "is_paid_subscription_active": True,
                        }
                    }
                }
            })),
        })
        state = probe_with_token("web-token", email="owner@example.com", session=session)
        self.assertTrue(state["verified"])
        self.assertEqual(state["plan"], "Plus")
        self.assertIs(state["is_plus"], True)
        self.assertEqual(state["evidence"]["raw"].keys() >= {"me", "accounts_check"}, True)

    def test_missing_token_never_issues_requests(self):
        session = FakeSession({})
        state = probe_with_token("", email="owner@example.com", session=session)
        self.assertEqual(session.calls, [])
        self.assertFalse(state["verified"])

    def test_browser_channel_parses_page_results(self):
        class FakeDriver:
            current_url = "https://chatgpt.com/"

            def __init__(self, payload):
                self.payload = payload
                self.timeouts = type("T", (), {"script": 30})()

            def set_script_timeout(self, _value):
                pass

            def get(self, _url):
                pass

            def execute_script(self, _script, *_args):
                return self.payload

        driver = FakeDriver({
            "__origin": "https://chatgpt.com",
            "session": {"status": 200, "text": json.dumps({"accessToken": "web-token", "user": {"id": "u-1"}})},
            "me": {"status": 200, "text": json.dumps({"account_plan": "chatgptplusplan"})},
            "accounts_check": {"status": 200, "text": json.dumps({
                "accounts": {"acc": {"entitlement": {
                    "subscription_plan": "chatgptplusplan",
                    "is_paid_subscription_active": True,
                }}}
            })},
        })
        state = probe_in_browser(driver, email="owner@example.com")
        self.assertTrue(state["verified"])
        self.assertEqual(state["plan"], "Plus")
        self.assertEqual(state["evidence"]["session"]["user_id"], "u-1")

    def test_browser_channel_reports_script_failure(self):
        class BrokenDriver:
            current_url = "https://chatgpt.com/"

            def set_script_timeout(self, _value):
                pass

            def execute_script(self, *_args):
                raise RuntimeError("detached")

        state = probe_in_browser(BrokenDriver(), email="owner@example.com")
        self.assertFalse(state["verified"])
        self.assertIn("execute_script_failed", state["sources"][0]["note"])


if __name__ == "__main__":
    unittest.main()
