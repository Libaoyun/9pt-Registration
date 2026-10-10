import types
import unittest
from unittest.mock import patch

from app.oauth_service import CodexOAuthClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None, url="https://auth.openai.com/log-in"):
        self.status_code = status_code
        self._payload = payload or {}
        self.url = url
        self.text = ""
        self.headers = {}

    def json(self):
        return self._payload


class FakeCookieJar:
    def __init__(self):
        self._cookies = []

    def set(self, *args, **kwargs):
        return None

    def __iter__(self):
        return iter(self._cookies)


class FakeSession:
    def __init__(self, post_responses):
        self.post_responses = list(post_responses)
        self.post_calls = []
        self.get_calls = []
        self.headers = {}
        self.cookies = FakeCookieJar()
        self.proxies = {}

    def post(self, url, headers=None, json=None, data=None, timeout=None, allow_redirects=None, **kwargs):
        self.post_calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "data": data,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        if not self.post_responses:
            raise AssertionError(f"unexpected POST {url}")
        return self.post_responses.pop(0)

    def get(self, url, headers=None, params=None, allow_redirects=None, timeout=None, **kwargs):
        self.get_calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "allow_redirects": allow_redirects,
                "timeout": timeout,
            }
        )
        return FakeResponse(status_code=200, url="https://auth.openai.com/log-in")


class OAuthBranchingTests(unittest.TestCase):
    @patch("app.oauth_service._generate_pkce", return_value=("verifier", "challenge"))
    @patch("app.oauth_service._request_kwargs", return_value={})
    @patch("app.oauth_service.build_sentinel_token", side_effect=["authorize-token"])
    def test_perform_login_supports_no_password_email_otp_branch(
        self,
        mock_build_sentinel,
        mock_request_kwargs,
        mock_pkce,
    ):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    payload={
                        "continue_url": "/u/email-verification",
                        "page": {"type": "email_otp_verification"},
                    },
                ),
                FakeResponse(
                    200,
                    payload={
                        "continue_url": "https://auth.openai.com/authorize/resume?code=test-auth-code",
                        "page": {"type": "consent"},
                    },
                ),
                FakeResponse(
                    200,
                    payload={"access_token": "access-token", "refresh_token": "refresh-token"},
                ),
            ]
        )

        with patch("app.oauth_service._new_session", return_value=session):
            client = CodexOAuthClient(proxy=None)

        with patch.object(client, "_bootstrap_oauth_session", return_value=("https://auth.openai.com/log-in", True)):
            with patch("app.email_providers.list_verification_codes", return_value=["123456"]):
                result = client.perform_login(
                    email="user@example.com",
                    password="N/A",
                    email_provider="custom2925",
                    mail_token="mail-session",
                )

        self.assertEqual(result["access_token"], "access-token")
        posted_urls = [call["url"] for call in session.post_calls]
        self.assertIn("https://auth.openai.com/api/accounts/authorize/continue", posted_urls)
        self.assertIn("https://auth.openai.com/api/accounts/email-otp/validate", posted_urls)
        self.assertIn("https://auth.openai.com/oauth/token", posted_urls)
        self.assertNotIn("https://auth.openai.com/api/accounts/password/verify", posted_urls)


if __name__ == "__main__":
    unittest.main()
