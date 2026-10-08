"""Herdr CLI adapter: explicit selection and validated response envelopes."""

import json
from pathlib import Path

from commands import CommandRunner, READ_TIMEOUT, Reporter, display_text, git_environment


PLUGIN_ID = "herdr.worktree-tools"


class HerdrError(Exception):
    pass


class HerdrClient:
    def __init__(self, executable: str = "herdr", runner: CommandRunner | None = None):
        self.executable = executable
        self.runner = runner if runner is not None else CommandRunner()

    @staticmethod
    def _selection(workspace_id: str | None) -> str:
        if (not workspace_id or workspace_id.startswith("-")
                or any(ord(char) < 32 or ord(char) == 127 for char in workspace_id)):
            raise HerdrError("Focus a linked worktree, then invoke the Merge & delete worktree plugin action.")
        return workspace_id

    def _request(self, *args: str, expected: str, mutating: bool = False,
                 report: Reporter | None = None) -> dict:
        raw = self.runner.run([self.executable, *args], env=git_environment(),
                              timeout=None if mutating else READ_TIMEOUT, report=report)
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise HerdrError("Herdr returned an invalid JSON response. Inspect its logs before retrying.") from error
        if not isinstance(response, dict):
            raise HerdrError("Herdr returned an invalid response envelope.")
        if "error" in response:
            raise HerdrError(f"Herdr stopped: {display_text(str(response['error']))}")
        result = response.get("result")
        if not isinstance(result, dict) or result.get("type") != expected:
            raise HerdrError(f"Herdr returned an unexpected response; expected {expected}.")
        return result

    @staticmethod
    def _path(value: object) -> Path:
        if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
            raise HerdrError("Herdr returned an invalid checkout path.")
        return Path(value).resolve()

    def workspace(self, workspace_id: str | None) -> tuple[Path, Path]:
        selected = self._selection(workspace_id)
        result = self._request("workspace", "get", selected, expected="workspace_info")
        workspace = result.get("workspace")
        if not isinstance(workspace, dict) or workspace.get("workspace_id") != selected:
            raise HerdrError("Herdr returned a different or invalid selected workspace.")
        worktree = workspace.get("worktree")
        if not isinstance(worktree, dict) or worktree.get("is_linked_worktree") is not True:
            raise HerdrError("Select a linked worktree, not the main checkout.")
        return self._path(worktree.get("checkout_path")), self._path(worktree.get("repo_root"))

    def open_popup(self, workspace_id: str | None) -> None:
        selected = self._selection(workspace_id)
        self.workspace(selected)
        self._request(
            "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "merge-delete",
            "--workspace", selected, "--env", f"HERDR_MERGE_WORKSPACE_ID={selected}", "--focus",
            expected="plugin_pane_opened", mutating=True,
        )

    def remove(self, workspace_id: str, source: Path, report: Reporter) -> None:
        selected = self._selection(workspace_id)
        result = self._request("worktree", "remove", "--workspace", selected,
                               expected="worktree_removed", mutating=True, report=report)
        if (result.get("workspace_id") != selected or self._path(result.get("path")) != source
                or result.get("forced") is not False):
            raise HerdrError("Herdr's removal response did not match the reviewed, non-forced removal. Inspect its logs.")
