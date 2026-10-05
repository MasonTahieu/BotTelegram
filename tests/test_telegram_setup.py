"""Use fake tokens and a hidden window; never access the user's clipboard/API."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fintech_bot.bot.telegram import store_token
from fintech_bot.bot.telegram_setup import TelegramSetupWindow

FAKE_TOKEN = "123456:" + "x" * 30


class FakeClient:
    def __init__(self, token):
        self.token = token

    def verify(self):
        return {"username": "test_setup_bot", "is_bot": True}


class StorageTests(unittest.TestCase):
    def test_atomic_save_and_failed_replace_keep_previous_token(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret" / "token.txt"
            store_token(FAKE_TOKEN, path)
            self.assertEqual(path.read_text(encoding="utf-8"), FAKE_TOKEN + "\n")
            with patch("fintech_bot.bot.telegram.os.replace", side_effect=OSError("Simulated failure")):
                with self.assertRaises(OSError):
                    store_token("different-test-token", path)
            self.assertEqual(path.read_text(encoding="utf-8"), FAKE_TOKEN + "\n")
            self.assertEqual([p.name for p in path.parent.iterdir()], ["token.txt"])


class WindowTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
            self.root = tk.Tk()
        except Exception as error:
            self.skipTest("Tk window unavailable: " + type(error).__name__)
        self.root.withdraw()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "token.txt"
        self.dialog = TelegramSetupWindow(self.root, self.path, client_factory=FakeClient)
        self.addCleanup(lambda: self.dialog.close() if not self.dialog.closed else None)

    def test_typing_mask_character_count_visibility_and_selection(self):
        self.dialog.entry.insert(0, FAKE_TOKEN)
        self.assertEqual(self.dialog.token.get(), FAKE_TOKEN)
        self.assertEqual(self.dialog.entry.cget("show"), "*")
        self.assertIn(str(len(FAKE_TOKEN)), self.dialog.count.get())
        self.dialog.show_token.set(True)
        self.dialog.toggle_visibility()
        self.assertEqual(self.dialog.entry.cget("show"), "")
        self.dialog.select_all()
        self.assertTrue(self.dialog.entry.selection_present())

    def test_paste_button_uses_clipboard_only_when_clicked_and_trims_spaces(self):
        with patch.object(self.root, "clipboard_get", return_value="  " + FAKE_TOKEN + "\n") as clipboard:
            clipboard.assert_not_called()
            self.dialog.paste_button.invoke()
            clipboard.assert_called_once()
        self.assertEqual(self.dialog.token.get(), FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, self.dialog.status.get())
        self.assertFalse(self.path.exists())

    def test_empty_and_invalid_input_do_not_start_verification(self):
        with patch("fintech_bot.bot.telegram_setup.threading.Thread") as worker:
            self.dialog.submit()
            self.assertIn("chưa nhập", self.dialog.status.get())
            self.dialog.token.set("not a token")
            with patch.object(self.dialog, "client_factory", side_effect=ValueError()):
                self.dialog.submit()
            self.assertIn("định dạng", self.dialog.status.get())
            worker.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_verification_runs_in_background_then_saves_and_clears_entry(self):
        self.dialog.token.set(FAKE_TOKEN)
        with patch("fintech_bot.bot.telegram_setup.threading.Thread") as worker:
            self.dialog.submit()
            self.dialog.submit()
            worker.assert_called_once()
            worker.return_value.start.assert_called_once()
            self.assertTrue(self.dialog.busy)
            self.assertIn("disabled", self.dialog.save_button.state())
            self.assertFalse(self.path.exists())
        self.dialog.verify(FakeClient(FAKE_TOKEN), FAKE_TOKEN)
        self.root.after_cancel(self.dialog.poll_job)
        self.dialog.poll()
        self.assertFalse(self.dialog.busy)
        self.assertTrue(self.dialog.saved)
        self.assertEqual(self.dialog.token.get(), "")
        self.assertEqual(self.path.read_text(encoding="utf-8").strip(), FAKE_TOKEN)
        self.assertIn("@test_setup_bot", self.dialog.status.get())

    def test_error_preserves_existing_token_and_does_not_expose_unknown_exception(self):
        self.path.write_text("old-test-token", encoding="utf-8")
        class BrokenClient:
            def verify(self):
                raise OSError("https://example.invalid/" + FAKE_TOKEN)
        self.dialog.verify(BrokenClient(), FAKE_TOKEN)
        self.dialog.poll()
        self.assertNotIn(FAKE_TOKEN, self.dialog.status.get())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old-test-token")
        self.assertFalse(self.dialog.saved)

    def test_closing_while_verification_runs_does_not_save_a_token(self):
        self.dialog.token.set(FAKE_TOKEN)
        with patch("fintech_bot.bot.telegram_setup.threading.Thread"):
            self.dialog.submit()
        self.dialog.close()
        self.dialog.verify(FakeClient(FAKE_TOKEN), FAKE_TOKEN)
        self.dialog.poll()
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
