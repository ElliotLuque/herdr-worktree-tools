"""Transport contracts: bytes, diagnostics, streaming, and interaction policy."""

import itertools
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from commands import CommandError, CommandRunner, DIAGNOSTIC_LIMIT, Git, display_path, display_text, git_environment


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.runner = CommandRunner()

    def python(self, code, **options):
        return self.runner.run([sys.executable, "-E", "-s", "-c", code], **options)

    def test_capture_preserves_non_utf8_nul_and_carriage_returns(self):
        data = self.python("import os; os.write(1, b'\\xff\\0\\r\\n\\n')")
        self.assertEqual(data, b"\xff\0\r\n\n")

    def test_stdin_is_closed_for_noninteractive_hooks(self):
        data = self.python("import sys; print(repr(sys.stdin.read()))", timeout=None)
        self.assertEqual(data, b"''\n")

    def test_child_has_no_controlling_terminal(self):
        data = self.python("import os; print(os.getsid(0) == os.getpid())")
        self.assertEqual(data, b"True\n")

    def test_failure_retains_both_streams_and_escapes_controls(self):
        with self.assertRaises(CommandError) as caught:
            self.python("import os; os.write(1,b'CONFLICT file\\n'); os.write(2,b'\\x1b[31mwarning\\xff\\n'); exit(2)")
        error = caught.exception
        self.assertEqual(error.returncode, 2)
        self.assertEqual(error.stdout, b"CONFLICT file\n")
        self.assertIn("CONFLICT file", str(error))
        self.assertIn("\\x1b[31mwarning", str(error))
        self.assertNotIn("\x1b", str(error))

    def test_read_only_command_deadline_reports_error(self):
        with self.assertRaisesRegex(CommandError, "Read-only command timed out"):
            self.python("import time; time.sleep(5)", timeout=0.03)

    def test_streaming_reports_output_before_process_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            released = Path(directory) / "released"
            messages = []
            def report(text):
                messages.append(text)
                if text == "before":
                    released.touch()
            code = ("from pathlib import Path\nimport time\nprint('before',flush=True)\n"
                    f"path=Path({str(released)!r})\ndeadline=time.monotonic()+3\n"
                    "while not path.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                    "assert path.exists(), 'output was not streamed'\nprint('after',flush=True)\n")
            data = self.python(code, timeout=None, report=report)
        self.assertEqual(data, b"before\nafter\n")
        self.assertEqual(messages, ["before", "after"])

    def test_streaming_drains_both_pipes_and_bounds_diagnostics(self):
        data = self.python("import os; os.write(1,b'x'*2097152); os.write(2,b'y'*2097152)",
                           timeout=None, report=lambda text: None)
        self.assertEqual(len(data), DIAGNOSTIC_LIMIT)

    def test_failed_ui_reporting_does_not_interrupt_child(self):
        def report(text):
            raise RuntimeError("panel closed")
        self.assertEqual(self.python("print('still completed')", timeout=None, report=report), b"still completed\n")

    def test_slow_command_emits_running_notice(self):
        messages = []
        times = itertools.chain([0.0, 11.0], itertools.repeat(11.0))
        with patch("commands.time.monotonic", side_effect=lambda: next(times)):
            self.python("print('completed')", timeout=None, report=messages.append)
        self.assertTrue(any("Still running" in text for text in messages))

    def test_git_mutations_have_no_deadline_and_metadata_is_byte_preserving(self):
        with patch.object(self.runner, "run", return_value=b"/checkout\n\n") as run:
            git = Git(self.runner)
            self.assertEqual(git.path(Path("/"), "rev-parse", "--show-toplevel"), Path("/checkout\n"))
            self.assertIsNotNone(run.call_args.kwargs["timeout"])
            git.raw(Path("/"), "commit", "-m", "message", mutating=True)
            self.assertIsNone(run.call_args.kwargs["timeout"])
            self.assertIn("--no-replace-objects", run.call_args.args[0])

    def test_git_environment_removes_overrides_but_keeps_session_and_config_selection(self):
        env = {"GIT_DIR": "/bad/repo", "GIT_CONFIG_PARAMETERS": "bad", "GIT_CONFIG_VALUE_0": "bad",
               "GIT_CONFIG_GLOBAL": "/dev/null", "HERDR_SOCKET_PATH": "/selected/session.sock",
               "GIT_TERMINAL_PROMPT": "1"}
        with patch.dict(os.environ, env, clear=True):
            cleaned = git_environment()
        self.assertNotIn("GIT_DIR", cleaned)
        self.assertNotIn("GIT_CONFIG_PARAMETERS", cleaned)
        self.assertNotIn("GIT_CONFIG_VALUE_0", cleaned)
        self.assertEqual(cleaned["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(cleaned["HERDR_SOCKET_PATH"], "/selected/session.sock")
        self.assertEqual(cleaned["GIT_TERMINAL_PROMPT"], "0")

    def test_display_escapes_paths_bidi_and_filesystem_surrogates(self):
        self.assertEqual(display_path(Path("/a\r\n\t\udcff")), "/a\\x0d\\n\\t\\xff")
        self.assertEqual(display_text("hello\u202e\x1b[2J"), "hello\\u202e\\x1b[2J")
        self.assertEqual(display_text("line\nnext\tcolumn"), "line\nnext\tcolumn")


if __name__ == "__main__":
    unittest.main()
