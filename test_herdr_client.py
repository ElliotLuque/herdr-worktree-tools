"""Herdr response validation without a running server or user workspaces."""

import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from commands import CommandRunner
from herdr_client import HerdrClient, HerdrError


def encoded(value):
    return json.dumps(value).encode("utf-8")


class HerdrClientTests(unittest.TestCase):
    def setUp(self):
        self.runner = Mock(spec=CommandRunner)
        self.client = HerdrClient("test-herdr", self.runner)
        self.workspace_id = "ws_selected"

    def workspace_response(self):
        return {"result": {"type": "workspace_info", "workspace": {
            "workspace_id": self.workspace_id,
            "worktree": {"is_linked_worktree": True, "checkout_path": "/repo/feature", "repo_root": "/repo/main"}}}}

    def test_valid_selection_returns_absolute_paths(self):
        self.runner.run.return_value = encoded(self.workspace_response())
        self.assertEqual(self.client.workspace(self.workspace_id), (Path("/repo/feature"), Path("/repo/main")))
        self.assertEqual(self.runner.run.call_args.args[0], ["test-herdr", "workspace", "get", self.workspace_id])
        self.assertIsNotNone(self.runner.run.call_args.kwargs["timeout"])

    def test_missing_or_option_like_selection_never_runs_command(self):
        for selected in (None, "", "--force", "ws_bad\0", "ws_bad\n"):
            with self.subTest(selected=selected), self.assertRaises(HerdrError):
                self.client.workspace(selected)
        self.runner.run.assert_not_called()

    def test_invalid_envelopes_fail_closed(self):
        values = [None, [], "response", {}, {"result": None}, {"result": []},
                  {"result": {"type": "ok"}}, {"error": {"code": "busy"}},
                  {"result": {"type": "workspace_info", "workspace": None}}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(HerdrError):
                self.runner.run.return_value = encoded(value)
                self.client.workspace(self.workspace_id)

    def test_invalid_json_or_encoding_fails_closed(self):
        for raw in (b"not JSON", b'{"result":"\xff"}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(HerdrError, "invalid JSON"):
                self.runner.run.return_value = raw
                self.client.workspace(self.workspace_id)

    def test_different_workspace_is_rejected(self):
        value = self.workspace_response()
        value["result"]["workspace"]["workspace_id"] = "ws_other"
        self.runner.run.return_value = encoded(value)
        with self.assertRaisesRegex(HerdrError, "different"):
            self.client.workspace(self.workspace_id)

    def test_non_boolean_linked_flag_is_rejected(self):
        for linked in (False, "true", "false", 1, None):
            with self.subTest(linked=linked), self.assertRaisesRegex(HerdrError, "linked worktree"):
                value = self.workspace_response()
                value["result"]["workspace"]["worktree"]["is_linked_worktree"] = linked
                self.runner.run.return_value = encoded(value)
                self.client.workspace(self.workspace_id)

    def test_invalid_checkout_paths_are_rejected(self):
        for path in (None, 10, "", "relative/path", "/repo/\0bad"):
            with self.subTest(path=path), self.assertRaisesRegex(HerdrError, "checkout path"):
                value = self.workspace_response()
                value["result"]["workspace"]["worktree"]["checkout_path"] = path
                self.runner.run.return_value = encoded(value)
                self.client.workspace(self.workspace_id)

    def test_popup_uses_captured_selection_and_no_mutation_deadline(self):
        self.runner.run.side_effect = [encoded(self.workspace_response()),
                                       encoded({"result": {"type": "plugin_pane_opened"}})]
        self.client.open_popup(self.workspace_id)
        args = self.runner.run.call_args.args[0]
        self.assertIn(f"HERDR_MERGE_WORKSPACE_ID={self.workspace_id}", args)
        self.assertIsNone(self.runner.run.call_args.kwargs["timeout"])

    def test_removal_requires_matching_path_workspace_and_nonforced_result(self):
        correct = {"type": "worktree_removed", "workspace_id": self.workspace_id,
                   "path": "/repo/feature", "forced": False}
        for update in ({"workspace_id": "ws_other"}, {"path": "/repo/other"},
                       {"path": "relative"}, {"forced": True}, {"forced": None}, {"forced": 0}, {"type": "ok"}):
            with self.subTest(update=update), self.assertRaises(HerdrError):
                self.runner.run.return_value = encoded({"result": {**correct, **update}})
                self.client.remove(self.workspace_id, Path("/repo/feature"), lambda text: None)
        self.runner.run.return_value = encoded({"result": correct})
        self.client.remove(self.workspace_id, Path("/repo/feature"), lambda text: None)
        self.assertIsNone(self.runner.run.call_args.kwargs["timeout"])
        self.assertNotIn("--force", self.runner.run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
