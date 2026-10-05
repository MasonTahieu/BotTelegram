import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fintech_bot.config import PROJECT_ROOT


class CliTests(unittest.TestCase):
    def test_interactive_local_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "session.sqlite3"
            result = subprocess.run(
                [sys.executable, "-m", "fintech_bot", "chat", "--timeframe", "1d", "--db", str(database)],
                input="/subscribe FPT\n/scan\n/scan\n/stock HPG\n/exit\n",
                cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("HPG: BÁN", result.stdout)
            self.assertEqual(result.stdout.count("THÔNG BÁO LOCAL cho local-demo"), 1)
            self.assertIn("Đã đóng phiên mô phỏng", result.stdout)

    def test_invalid_database_returns_clear_startup_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "invalid.sqlite3"
            database.write_text("This is not a SQLite database.", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "fintech_bot", "demo", "--db", str(database)],
                cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Không khởi động được", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
