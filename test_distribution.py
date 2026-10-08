"""Portable marketplace manifest, build, and launcher contracts (no network)."""

import importlib.metadata
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import install as installer


ROOT = Path(__file__).resolve().parent


class DistributionTests(unittest.TestCase):
    def test_manifest_is_installable_without_nix_substitution(self):
        source = (ROOT / "herdr-plugin.toml").read_text()
        self.assertNotIn("@command@", source)
        self.assertNotIn("/nix/store/", source)
        manifest = tomllib.loads(source)
        for field in ("id", "name", "version", "min_herdr_version"):
            self.assertTrue(manifest[field])
        self.assertEqual(manifest["platforms"], ["linux", "macos"])
        self.assertEqual(manifest["build"][0]["command"], ["python3", "-E", "-s", "install.py"])
        self.assertEqual(manifest["actions"][0]["contexts"], ["workspace"])
        self.assertEqual(manifest["actions"][0]["command"], ["sh", "launch.sh", "--open"])
        self.assertEqual(manifest["panes"][0]["command"], ["sh", "launch.sh"])
        self.assertEqual((manifest["panes"][0]["width"], manifest["panes"][0]["height"]), (64, 18))
        for name in ("install.py", "launch.sh", "requirements.txt", "requirements.lock", "README.md", "PUBLISHING.md",
                     "LICENSE", "merge_delete.py", "worktree_workflow.py", "commands.py", "herdr_client.py", "merge_delete_ui.py"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_pinned_textual_matches_tested_runtime(self):
        version = importlib.metadata.version("textual")
        self.assertIn(f"textual=={version}", (ROOT / "requirements.txt").read_text().splitlines())
        lock = (ROOT / "requirements.lock").read_text().replace("\\\n", "")
        lines = [line for line in lock.splitlines() if line and not line.startswith("#")]
        self.assertTrue(any(line.startswith(f"textual=={version} ") for line in lines))
        for line in lines:
            self.assertRegex(line, r"^[\w-]+==[\d.]+\s+--hash=sha256:[a-f0-9]{64}$")
        self.assertEqual(len(lines), 9)

    def test_installer_uses_local_venv_and_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(installer.shutil, "which", return_value="/usr/bin/git"), \
                    patch.object(installer.venv, "EnvBuilder") as builder, \
                    patch.object(installer.subprocess, "run") as run:
                run.return_value.stdout = "git version 2.48.1\n"
                installer.install(root)
                builder.assert_called_once_with(with_pip=True)
                builder.return_value.create.assert_called_once_with(root / ".venv")
                self.assertEqual(run.call_args.args[0], [
                    str(root / ".venv/bin/python3"), "-E", "-s", "-m", "pip", "--isolated", "install", "--no-input",
                    "--disable-pip-version-check", "--require-hashes", "--only-binary=:all:",
                    "--requirement", str(root / "requirements.lock"),
                ])

    def test_missing_or_old_git_aborts_before_creating_venv(self):
        with patch.object(installer.venv, "EnvBuilder") as builder:
            with patch.object(installer.shutil, "which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "Git 2.38"):
                    installer.install(ROOT)
            with patch.object(installer.shutil, "which", return_value="git"), \
                    patch.object(installer.subprocess, "run") as run:
                run.return_value.stdout = "git version 2.37.4\n"
                with self.assertRaisesRegex(RuntimeError, "Git 2.38"):
                    installer.install(ROOT)
            builder.assert_not_called()

    def launch(self, root, *args):
        env = dict(os.environ, HERDR_PLUGIN_ROOT=str(root))
        return subprocess.run(["sh", str(root / "launch.sh"), *args], cwd="/", env=env,
                              text=True, capture_output=True)

    def test_launcher_supports_venv_and_nix_and_quotes_paths(self):
        with tempfile.TemporaryDirectory(prefix="worktree tools ") as directory:
            root = Path(directory)
            shutil.copy(ROOT / "launch.sh", root)
            python = root / ".venv/bin/python3"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
            python.chmod(0o755)
            result = self.launch(root, "--open")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["-E", "-s", str(root / "merge_delete.py"), "--open"])
            wrapped = root / "bin/merge-delete"
            wrapped.parent.mkdir()
            wrapped.write_text("#!/bin/sh\nprintf 'wrapped:%s\\n' \"$@\"\n")
            wrapped.chmod(0o755)
            result = self.launch(root, "--open")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "wrapped:--open\n")

    def test_launcher_ignores_python_import_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(ROOT / "launch.sh", root)
            python = root / ".venv/bin/python3"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            (root / "merge_delete.py").write_text(
                "import sys\nprint(sys.flags.ignore_environment, sys.flags.no_user_site)\n")
            env = dict(os.environ, HERDR_PLUGIN_ROOT=str(root), PYTHONHOME="/missing/python", PYTHONPATH="/bad/imports")
            result = subprocess.run(["sh", str(root / "launch.sh")], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "1 1\n")

    def test_ci_actions_are_pinned_to_commit_ids(self):
        workflow = (ROOT / ".github/workflows/tests.yml").read_text()
        actions = re.findall(r"uses: ([^\s]+)", workflow)
        self.assertEqual(len(actions), 2)
        for action in actions:
            self.assertRegex(action, r"^actions/[\w-]+@[a-f0-9]{40}$")

    def test_unbuilt_launcher_reports_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(ROOT / "launch.sh", root)
            result = self.launch(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("python3 -E -s install.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
