import tempfile
import textwrap
import unittest

from app.config import ConfigLoader


class ConfigOAuthTests(unittest.TestCase):
    def test_loader_parses_oauth_and_cpa_sections(self):
        yaml_content = textwrap.dedent(
            """
            oauth:
              enabled: true
              required: false
              issuer: "https://auth.example.com"
              client_id: "client_123"
              redirect_uri: "http://localhost:1455/auth/callback"
              ak_file: "tokens/ak.txt"
              rk_file: "tokens/rk.txt"
              token_json_dir: "tokens/json"

            cpa:
              upload_api_url: "https://cpa.example.com/upload"
              upload_api_token: "secret-token"
            """
        )

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(yaml_content)
            config_path = handle.name

        loader = ConfigLoader(config_path=config_path)

        self.assertTrue(loader.config.oauth.enabled)
        self.assertFalse(loader.config.oauth.required)
        self.assertEqual(loader.config.oauth.issuer, "https://auth.example.com")
        self.assertEqual(loader.config.oauth.client_id, "client_123")
        self.assertEqual(
            loader.config.oauth.redirect_uri,
            "http://localhost:1455/auth/callback",
        )
        self.assertEqual(loader.config.oauth.ak_file, "tokens/ak.txt")
        self.assertEqual(loader.config.oauth.rk_file, "tokens/rk.txt")
        self.assertEqual(loader.config.oauth.token_json_dir, "tokens/json")
        self.assertEqual(
            loader.config.cpa.upload_api_url,
            "https://cpa.example.com/upload",
        )
        self.assertEqual(loader.config.cpa.upload_api_token, "secret-token")


if __name__ == "__main__":
    unittest.main()
