"""Byte-preserving subprocess transport for the supported POSIX platforms.

Read-only commands have a deadline. Mutations never have an automatic timeout:
interrupting a commit, hook, merge, or removal does not make it safe to retry.
"""

from collections.abc import Callable, Mapping, Sequence
import os
from pathlib import Path
import selectors
import subprocess
import time


Reporter = Callable[[str], None]
READ_TIMEOUT = 30.0
DIAGNOSTIC_LIMIT = 1024 * 1024


def display_text(value: str) -> str:
    """Escape terminal controls, bidi controls, and filesystem surrogate bytes."""
    result = []
    for char in value:
        code = ord(char)
        if 0xDC80 <= code <= 0xDCFF:
            result.append(f"\\x{code - 0xDC00:02x}")
        elif (code < 32 and char not in "\n\t") or 127 <= code <= 159:
            result.append(f"\\x{code:02x}")
        elif 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069 or 0xD800 <= code <= 0xDFFF:
            result.append(f"\\u{code:04x}")
        else:
            result.append(char)
    return "".join(result)


def display_path(path: Path) -> str:
    return display_text(str(path)).replace("\n", "\\n").replace("\t", "\\t")


def git_environment() -> dict[str, str]:
    """Do not let inherited Git routing/index overrides redirect this workflow.

    Keep explicit config-file selection (including CI's /dev/null isolation),
    ordinary user configuration, signing agents, and Herdr socket/session context.
    This is not a sandbox: Git hooks and configured merge drivers remain trusted.
    """
    config_selection = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") or key in config_selection}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return env


class CommandError(Exception):
    def __init__(self, argv: Sequence[str], stdout: bytes = b"", stderr: bytes = b"",
                 returncode: int | None = None, *, reason: str | None = None):
        self.argv = tuple(argv)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        # Preserve both streams: merge-tree often describes conflicts on stdout.
        diagnostics = b"\n".join(part.strip() for part in (stderr, stdout) if part.strip())
        message = reason or diagnostics.decode("utf-8", errors="replace") or f"{argv[0]} failed ({returncode})"
        super().__init__(display_text(message))


class CommandRunner:
    def run(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None,
            timeout: float | None = READ_TIMEOUT, report: Reporter | None = None) -> bytes:
        """Capture exact bytes; optionally stream bounded, escaped diagnostics.

        stdin is closed and children have no controlling terminal. Output reporting is informational;
        a failed UI callback must not interrupt a mutating child process.
        """
        if report is None:
            try:
                result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                        env=env, timeout=timeout, start_new_session=True)
            except subprocess.TimeoutExpired as error:
                raise CommandError(argv, error.stdout or b"", error.stderr or b"",
                                   reason=f"Read-only command timed out after {timeout:g}s: {argv[0]}") from error
            except OSError as error:
                raise CommandError(argv, reason=str(error)) from error
            if result.returncode:
                raise CommandError(argv, result.stdout, result.stderr, result.returncode)
            return result.stdout
        return self._stream(argv, env=env, timeout=timeout, report=report)

    def _stream(self, argv: Sequence[str], *, env: Mapping[str, str] | None,
                timeout: float | None, report: Reporter) -> bytes:
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=env, start_new_session=True)
        except OSError as error:
            raise CommandError(argv, reason=str(error)) from error
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        pending = {"stdout": bytearray(), "stderr": bytearray()}
        started = time.monotonic()
        next_notice = started + 10

        def emit(data: bytes) -> None:
            text = display_text(data.decode("utf-8", errors="replace"))
            if text:
                try:
                    report(text)
                except Exception:
                    # A closed panel cannot make an in-flight Git operation safe to kill.
                    pass

        with process, selectors.DefaultSelector() as selector:
            for name in buffers:
                pipe = getattr(process, name)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if timeout is not None and now - started >= timeout:
                    process.kill()
                    process.wait()
                    raise CommandError(argv, bytes(buffers["stdout"]), bytes(buffers["stderr"]),
                                       reason=f"Read-only command timed out after {timeout:g}s: {argv[0]}")
                if now >= next_notice:
                    emit(f"Still running {argv[0]} ({int(now - started)}s). Hooks/signing may take time; keep the panel open.".encode())
                    next_notice = now + 10
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    name = key.data
                    if not chunk:
                        selector.unregister(key.fileobj)
                        emit(bytes(pending[name]))
                        pending[name].clear()
                        continue
                    buffers[name].extend(chunk)
                    if len(buffers[name]) > DIAGNOSTIC_LIMIT:
                        del buffers[name][:-DIAGNOSTIC_LIMIT]
                    pending[name].extend(chunk)
                    while b"\n" in pending[name] or len(pending[name]) >= 4096:
                        newline = pending[name].find(b"\n")
                        size = min(newline + 1 if newline >= 0 else len(pending[name]), 4096)
                        emit(bytes(pending[name][:size]).removesuffix(b"\n"))
                        del pending[name][:size]
            process.wait()
        stdout, stderr = bytes(buffers["stdout"]), bytes(buffers["stderr"])
        if process.returncode:
            raise CommandError(argv, stdout, stderr, process.returncode)
        return stdout


class Git:
    """Git transport policy, shared by planning, execution, and real-Git tests."""

    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner if runner is not None else CommandRunner()

    def raw(self, path: Path, *args: str, mutating: bool = False,
            report: Reporter | None = None) -> bytes:
        argv = ["git", "--no-pager", "--no-replace-objects", "-C", str(path),
                "-c", "core.fsmonitor=false", "-c", "core.quotePath=true", "-c", "color.ui=false", *args]
        return self.runner.run(argv, env=git_environment(), timeout=None if mutating else READ_TIMEOUT,
                               report=report)

    def text(self, path: Path, *args: str, mutating: bool = False,
             report: Reporter | None = None) -> str:
        # Remove exactly the command's terminator, not newlines belonging to paths.
        return os.fsdecode(self.raw(path, *args, mutating=mutating, report=report).removesuffix(b"\n"))

    def path(self, path: Path, *args: str) -> Path:
        return Path(self.text(path, *args))
