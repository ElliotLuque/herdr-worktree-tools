"""Herdr marketplace build: install UI dependencies in a plugin-local virtualenv."""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import venv


def install(root: Path) -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Worktree Tools needs Python 3.11 or newer.")
    if not shutil.which("git"):
        raise RuntimeError("Install Git 2.38 or newer before installing Worktree Tools.")
    version = subprocess.run(["git", "--version"], check=True, text=True, capture_output=True).stdout
    match = re.search(r"\b(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (2, 38):
        raise RuntimeError("Worktree Tools needs Git 2.38 or newer for safe merge preflights.")
    destination = root / ".venv"
    venv.EnvBuilder(with_pip=True).create(destination)
    subprocess.run([
        str(destination / "bin/python3"), "-E", "-s", "-m", "pip", "--isolated", "install",
        "--no-input", "--disable-pip-version-check", "--require-hashes", "--only-binary=:all:",
        "--requirement", str(root / "requirements.lock"),
    ], check=True)


if __name__ == "__main__":
    try:
        install(Path(__file__).resolve().parent)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Cannot build Worktree Tools: {error}", file=sys.stderr)
        print("Install Python with venv/pip support and Git, then retry. No Git checkout was changed.", file=sys.stderr)
        sys.exit(1)
