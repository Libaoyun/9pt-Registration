"""Run local regressions using workspace temp directories and mocked services."""
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent


def main():
    python = ROOT / "gpt-auto-register" / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = Path(sys.executable)
    for edition in ("gpt-auto-register", "gpt-protocol-register"):
        with tempfile.TemporaryDirectory(prefix="local-check-", dir=ROOT) as temp_path:
            if not Path(temp_path).resolve().is_relative_to(ROOT.resolve()):
                raise RuntimeError("Test temp directory is outside the workspace")
            code = (
                "import tempfile,unittest;"
                f"tempfile.tempdir={temp_path!r};"
                "result=unittest.TextTestRunner(verbosity=1).run("
                "unittest.defaultTestLoader.discover('tests'));"
                "raise SystemExit(not result.wasSuccessful())"
            )
            result = subprocess.run([str(python), "-c", code], cwd=ROOT / edition)
            if result.returncode:
                return result.returncode
    return subprocess.run(["node", str(ROOT / "scripts" / "verify_frontend.cjs")], cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
