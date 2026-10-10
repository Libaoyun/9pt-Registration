"""全自动流水线回归：阶段编排、报表字段、报表落盘。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.pipeline import AutoPipeline, write_report_files


def _write_accounts(path: Path):
    evidence = {
        "verified": True,
        "is_paid": False,
        "is_plus": False,
        "trial_eligible": True,
        "plan_raw": "free",
        "plan_source": "$.accounts.acc.entitlement.subscription_plan",
        "checked_at": "2026-10-08T12:00:00+08:00",
        "source": "official_evidence",
        "sources": [{"name": "accounts_check", "status": 200}],
        "evidence_file": "",
    }
    path.write_text(
        "trial@example.com|Pass1234!|2026-10-08 11:00:00|已注册|cred|mailtm|Free|"
        "已确认有试用资格 (Plus 1个月试用)|未知，请在官方页面确认|未知，未取得订阅到期时间|"
        "🟢 在线已验证|JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP|" + json.dumps(evidence, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


class ReportTests(unittest.TestCase):
    def test_report_rows_expose_every_requested_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.txt"
            _write_accounts(path)
            with patch("app.pipeline.cfg.files.accounts_file", str(path)):
                report = AutoPipeline(log=lambda *_: None).build_report()
            self.assertEqual(report["total"], 1)
            row = report["rows"][0]
            self.assertEqual(row["email"], "trial@example.com")
            self.assertEqual(row["password"], "Pass1234!")
            self.assertEqual(row["two_factor_secret"], "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")
            self.assertEqual(len(row["two_factor_now"]), 6)
            self.assertTrue(row["two_factor_now"].isdigit())
            self.assertEqual(row["two_factor_remaining"], 30 - int(__import__("time").time()) % 30)
            self.assertEqual(row["register_time"], "2026-10-08 11:00:00")
            self.assertIs(row["trial_eligible"], True)
            self.assertIn("已确认有试用资格", row["trial_status"])
            self.assertIs(row["is_plus"], False)
            self.assertTrue(row["verified"])
            self.assertTrue(row["otpauth_url"].startswith("otpauth://totp/"))

    def test_report_without_2fa_leaves_secret_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.txt"
            path.write_text(
                "no2fa@example.com|Pass|2026-10-08 11:00:00|已注册|cred|mailtm|未检测|待官方确认\n",
                encoding="utf-8",
            )
            with patch("app.pipeline.cfg.files.accounts_file", str(path)):
                report = AutoPipeline(log=lambda *_: None).build_report()
            row = report["rows"][0]
            self.assertEqual(row["two_factor_secret"], "")
            self.assertEqual(row["two_factor_now"], "")

    def test_write_report_files_creates_all_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.txt"
            _write_accounts(path)
            with patch("app.pipeline.cfg.files.accounts_file", str(path)):
                report = AutoPipeline(log=lambda *_: None).build_report()
            files = write_report_files(report, output_dir=tmp)
            for key in ("json", "accounts_2fa_txt", "csv", "markdown"):
                self.assertTrue(Path(files[key]).is_file(), key)
            text = Path(files["accounts_2fa_txt"]).read_text(encoding="utf-8").strip()
            self.assertEqual(
                text,
                "trial@example.com-----Pass1234!-----JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
            )
            payload = json.loads(Path(files["json"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["rows"][0]["email"], "trial@example.com")


class PipelineRunTests(unittest.TestCase):
    def test_pipeline_runs_all_four_phases_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.txt"
            path.write_text("", encoding="utf-8")
            phases = []

            def fake_register(email_provider="mailtm", **kwargs):
                # 模拟注册阶段写回一条真实记录
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        "new@example.com|Pass1234!|2026-10-08 11:00:00|已注册|cred|mailtm|"
                        "未检测|待官方确认|未知，请在官方页面确认|未知，未取得订阅到期时间|"
                        "⚪ 未在线验证|\n"
                    )
                return "new@example.com", "Pass1234!", True

            def fake_perfect(email, **kwargs):
                return {"email": email, "success": True}

            def fake_refresh(email, accounts_file, **kwargs):
                return {"email": email, "verified": True, "plan": "Free"}

            def phase_callback(phase, done, total, message):
                phases.append(phase)

            with patch("app.pipeline.cfg.files.accounts_file", str(path)), \
                 patch("app.pipeline._resolve_register_func", return_value=fake_register), \
                 patch("app.pipeline._resolve_perfect_func", return_value=fake_perfect), \
                 patch("app.pipeline.refresh_account_state", side_effect=fake_refresh), \
                 patch("app.pipeline.write_report_files", return_value={"json": "x"}):
                pipeline = AutoPipeline(log=lambda *_: None, phase_callback=phase_callback)
                report = pipeline.run(count=1, providers=["mailtm"], headless=True)

            for phase in ("register", "perfect", "verify", "report"):
                self.assertIn(phase, phases)
            summary = report["summary"]
            self.assertEqual(summary["registered_ok"], 1)
            self.assertEqual(summary["perfect_ok"], 1)
            self.assertEqual(summary["verified"], 1)
            self.assertEqual(report["total"], 1)
            self.assertEqual(report["rows"][0]["email"], "new@example.com")

    def test_pipeline_stops_when_user_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.txt"
            path.write_text("", encoding="utf-8")
            calls = []

            def fake_register(email_provider="mailtm", **kwargs):
                calls.append(email_provider)
                return None, None, False

            with patch("app.pipeline.cfg.files.accounts_file", str(path)), \
                 patch("app.pipeline._resolve_register_func", return_value=fake_register), \
                 patch("app.pipeline.write_report_files", return_value={}):
                pipeline = AutoPipeline(log=lambda *_: None, stop_check=lambda: True)
                report = pipeline.run(count=3, providers=["mailtm"])
            self.assertEqual(calls, [])
            self.assertEqual(report["summary"]["registered_ok"], 0)


if __name__ == "__main__":
    unittest.main()
