"""Real disposable Git repositories; fake Herdr injected at the command seam."""

import contextlib
import errno
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from commands import CommandError, CommandRunner, Git
from herdr_client import HerdrClient, HerdrError
import merge_delete as cli
import worktree_workflow as workflow


def response(value):
    return json.dumps(value).encode("utf-8")


class FixtureRunner(CommandRunner):
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []

    def run(self, argv, **options):
        self.calls.append((tuple(argv), options))
        if argv[0] == "fake-herdr":
            return self.fixture.herdr(*argv)
        return super().run(argv, **options)


class GitFixture(unittest.TestCase):
    def setUp(self):
        isolation = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
        isolation.start()
        self.addCleanup(isolation.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / "main checkout"
        self.source = self.root / "feature checkout"
        self.target.mkdir()
        self.git = Git().text
        self.git(self.target, "init", "-b", "main")
        self.git(self.target, "config", "user.name", "Test")
        self.git(self.target, "config", "user.email", "test@example.invalid")
        self.git(self.target, "config", "commit.gpgsign", "false")
        self.git(self.target, "config", "core.autocrlf", "false")
        self.commit(self.target, "base", "base\n")
        self.git(self.target, "worktree", "add", "-b", "feature", str(self.source))
        self.commit(self.source, "feature", "feature\n", filename="feature.txt")
        self.workspace_id = "ws_clicked_not_focused"
        self.selection = (self.source, self.target, True)
        self.removed = False
        self.runner = FixtureRunner(self)
        self.client = HerdrClient("fake-herdr", self.runner)
        self.operation = workflow.WorktreeWorkflow(self.client)

    def commit(self, path, message, content, filename="file.txt"):
        (path / filename).write_text(content)
        self.git(path, "add", "--", filename, mutating=True)
        self.git(path, "commit", "-m", message, mutating=True)

    def herdr(self, *args):
        if args[1:] == ("workspace", "get", self.workspace_id):
            source, target, linked = self.selection
            return response({"result": {"type": "workspace_info", "workspace": {
                "workspace_id": self.workspace_id, "worktree": {
                    "checkout_path": str(source), "repo_root": str(target), "is_linked_worktree": linked}}}})
        if args[1:] == ("worktree", "remove", "--workspace", self.workspace_id):
            self.git(self.target, "worktree", "remove", str(self.source), mutating=True)
            self.removed = True
            return response({"result": {"type": "worktree_removed", "workspace_id": self.workspace_id,
                                        "path": str(self.source), "forced": False}})
        self.fail(f"Unexpected Herdr call: {args}")

    def inspect(self):
        return self.operation.inspect(self.workspace_id)

    def execute(self, plan=None, message=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.operation.execute(self.workspace_id, plan or self.inspect(), message)

    def add_submodule(self):
        subrepo = self.root / "subrepo"
        subrepo.mkdir()
        self.git(subrepo, "init", "-b", "main")
        self.git(subrepo, "config", "user.name", "Test")
        self.git(subrepo, "config", "user.email", "test@example.invalid")
        self.git(subrepo, "config", "commit.gpgsign", "false")
        self.commit(subrepo, "submodule base", "base\n")
        self.git(self.source, "-c", "protocol.file.allow=always", "submodule", "add", str(subrepo), "sub")
        self.git(self.source, "commit", "-m", "Add submodule")


class MergeDeleteTests(GitFixture):
    def test_stock_plugin_action_passes_exact_workspace_to_popup(self):
        opened = []
        original = self.herdr
        def open_run(*args):
            if args[:4] == ("fake-herdr", "plugin", "pane", "open"):
                opened.append(args)
                return response({"result": {"type": "plugin_pane_opened"}})
            return original(*args)
        env = {"HERDR_WORKSPACE_ID": self.workspace_id, "HERDR_MERGE_WORKSPACE_ID": "stale_popup_selection"}
        with patch.dict(os.environ, env, clear=True), patch.object(self, "herdr", side_effect=open_run):
            self.assertEqual(cli.main(["--open"], client=self.client), 0)
        self.assertEqual(opened, [("fake-herdr", "plugin", "pane", "open", "--plugin", "herdr.worktree-tools",
                                  "--entrypoint", "merge-delete", "--workspace", self.workspace_id,
                                  "--env", f"HERDR_MERGE_WORKSPACE_ID={self.workspace_id}", "--focus")])
        self.assertFalse(self.removed)

    def test_action_without_workspace_never_guesses_selection(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(self.runner, "run") as run, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--open"], client=self.client), 1)
            run.assert_not_called()

    def test_action_rejects_main_checkout_before_opening_popup(self):
        self.selection = (self.target, self.target, False)
        with self.assertRaisesRegex(HerdrError, "linked worktree"):
            self.client.open_popup(self.workspace_id)
        self.assertEqual(len(self.runner.calls), 1)

    def test_action_reports_popup_api_errors(self):
        original = self.herdr
        def rejected(*args):
            if args[:4] == ("fake-herdr", "plugin", "pane", "open"):
                return response({"error": {"code": "ui_busy"}})
            return original(*args)
        with patch.object(self, "herdr", side_effect=rejected):
            with self.assertRaisesRegex(HerdrError, "ui_busy"):
                self.client.open_popup(self.workspace_id)
        self.assertFalse(self.removed)

    def test_fast_forward_removes_only_selected_checkout_and_keeps_branch(self):
        source_head = self.git(self.source, "rev-parse", "HEAD")
        result = self.execute()
        self.assertTrue(result.removed)
        self.assertTrue(self.removed)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.git(self.target, "rev-parse", "HEAD"), source_head)
        self.assertEqual(self.git(self.target, "rev-parse", "feature"), source_head)

    def test_divergent_history_preserves_both_branches(self):
        self.commit(self.target, "main change", "main\n", filename="main.txt")
        source_head = self.git(self.source, "rev-parse", "HEAD")
        target_head = self.git(self.target, "rev-parse", "HEAD")
        self.execute()
        for head in (source_head, target_head):
            self.git(self.target, "merge-base", "--is-ancestor", head, "HEAD")
        self.assertEqual(len(self.git(self.target, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)

    def test_conflict_does_not_touch_target_or_remove_worktree(self):
        self.commit(self.target, "main conflict", "main version\n")
        self.commit(self.source, "feature conflict", "feature version\n")
        before = self.git(self.target, "rev-parse", "HEAD")
        with self.assertRaises(workflow.ExecutionFailure) as caught:
            self.execute()
        self.assertEqual(caught.exception.phase, workflow.Phase.PREFLIGHT)
        self.assertIn("CONFLICT", str(caught.exception))
        self.assertFalse(self.removed)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.git(self.target, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(self.target, "status", "--porcelain"), "")

    def test_dirty_target_is_rejected(self):
        for filename in ("file.txt", "untracked.txt"):
            with self.subTest(filename=filename):
                path = self.target / filename
                before = path.read_bytes() if path.exists() else None
                path.write_text("dirty\n")
                with self.assertRaises(workflow.WorkflowError):
                    self.inspect()
                if before is None:
                    path.unlink()
                else:
                    path.write_bytes(before)
        self.assertFalse(self.removed)

    def test_in_progress_git_operation_is_rejected_even_with_dirty_source(self):
        (self.source / "new.txt").write_text("new\n")
        marker = Path(self.git(self.source, "rev-parse", "--git-path", "MERGE_HEAD"))
        marker.write_text(self.git(self.target, "rev-parse", "HEAD") + "\n")
        with self.assertRaisesRegex(workflow.WorkflowError, "existing Git operation"):
            self.execute(message="Do not commit during a merge")
        self.assertFalse(self.removed)

    def test_dirty_submodule_contents_must_be_committed_inside_submodule(self):
        self.add_submodule()
        for filename in ("file.txt", "untracked.txt"):
            with self.subTest(filename=filename):
                file = self.source / "sub" / filename
                old = file.read_text() if file.exists() else None
                file.write_text("dirty\n")
                with self.assertRaisesRegex(workflow.WorkflowError, "inside submodules"):
                    self.execute(message="Cannot commit nested contents")
                if old is None:
                    file.unlink()
                else:
                    file.write_text(old)
        self.assertFalse(self.removed)

    def test_clean_submodule_is_rejected_before_any_mutation(self):
        self.add_submodule()
        before = self.git(self.target, "rev-parse", "HEAD")
        with self.assertRaisesRegex(workflow.WorkflowError, "submodules.*no-force"):
            self.inspect()
        self.assertEqual(self.git(self.target, "rev-parse", "HEAD"), before)
        self.assertFalse(self.removed)

    def test_locked_worktree_is_rejected_before_any_mutation(self):
        self.git(self.target, "worktree", "lock", str(self.source))
        with self.assertRaisesRegex(workflow.WorkflowError, "locked"):
            self.inspect()
        self.assertFalse(self.removed)

    def test_detached_head_and_main_checkout_are_rejected(self):
        self.selection = (self.target, self.target, True)
        with self.assertRaises(workflow.WorkflowError):
            self.inspect()
        self.selection = (self.source, self.target, True)
        self.git(self.source, "checkout", "--detach")
        with self.assertRaisesRegex(workflow.WorkflowError, "detached HEAD"):
            self.inspect()

    def test_symbolic_head_outside_local_branches_is_rejected(self):
        self.git(self.source, "tag", "not-a-branch")
        self.git(self.source, "symbolic-ref", "HEAD", "refs/tags/not-a-branch")
        with self.assertRaisesRegex(workflow.WorkflowError, "local branches"):
            self.inspect()
        self.assertFalse(self.removed)

    def test_foreign_repo_is_rejected(self):
        other = self.root / "other"
        other.mkdir()
        self.git(other, "init", "-b", "main")
        self.git(other, "config", "user.name", "Test")
        self.git(other, "config", "user.email", "test@example.invalid")
        self.commit(other, "base", "other\n")
        self.selection = (self.source, other, True)
        with self.assertRaisesRegex(workflow.WorkflowError, "different repositories"):
            self.inspect()

    def test_changed_source_or_target_after_confirmation_is_rejected(self):
        for path in (self.source, self.target):
            with self.subTest(path=path):
                plan = self.inspect()
                self.commit(path, "concurrent change", "concurrent\n", filename="concurrent.txt")
                with self.assertRaisesRegex(workflow.WorkflowError, "changed since confirmation"):
                    self.execute(plan)
                self.assertFalse(self.removed)

    def test_workspace_retargeting_after_confirmation_is_rejected(self):
        plan = self.inspect()
        self.selection = (self.target, self.target, False)
        with self.assertRaises(workflow.ExecutionFailure) as caught:
            self.execute(plan)
        self.assertFalse(caught.exception.result.removal_attempted)
        self.assertEqual(caught.exception.phase, workflow.Phase.VALIDATE)

    def test_merge_failure_keeps_checkout(self):
        hook = self.target / ".git/hooks/pre-merge-commit"
        hook.write_text("#!/bin/sh\necho 'merge rejected' >&2\nexit 1\n")
        hook.chmod(0o755)
        self.commit(self.target, "diverge", "main\n", filename="main.txt")
        with self.assertRaises(workflow.ExecutionFailure) as caught:
            self.execute()
        self.assertEqual(caught.exception.phase, workflow.Phase.MERGE)
        self.assertFalse(caught.exception.result.removal_attempted)
        self.assertFalse(self.removed)
        self.assertTrue(self.source.exists())

    def test_cleanup_failure_keeps_successful_merge_and_reports_recovery(self):
        original = self.herdr
        def fail_remove(*args):
            if args[:3] == ("fake-herdr", "worktree", "remove"):
                raise CommandError(args, stderr=b"checkout locked", returncode=1)
            return original(*args)
        plan = self.inspect()
        with patch.object(self, "herdr", side_effect=fail_remove):
            with self.assertRaisesRegex(workflow.ExecutionFailure, "checkout locked") as caught:
                self.execute(plan)
        failure = caught.exception
        self.assertEqual(failure.phase, workflow.Phase.REMOVE)
        self.assertTrue(failure.result.removal_attempted)
        self.assertIn(workflow.Phase.MERGE, failure.result.completed)
        self.assertEqual(failure.result.confirmed_target_head, plan.target_head)
        self.assertIn(str(self.target), failure.result.recovery())
        self.assertTrue(self.source.exists())
        self.git(self.target, "merge-base", "--is-ancestor", plan.source_head, "HEAD")

    def test_commits_staged_unstaged_and_untracked_then_merges(self):
        (self.source / "file.txt").write_text("staged\n")
        self.git(self.source, "add", "file.txt")
        (self.source / "file.txt").write_text("unstaged after staging\n")
        (self.source / "new file.txt").write_text("new\n")
        self.git(self.source, "config", "core.excludesFile", str(self.root / "ignore"))
        (self.root / "ignore").write_text("ignored.txt\n")
        (self.source / "ignored.txt").write_text("not committed\n")
        result = self.execute(message="Finish the feature")
        self.assertIn(workflow.Phase.COMMIT, result.completed)
        self.assertIsNotNone(result.committed_head)
        self.assertTrue(self.removed)
        self.assertEqual(self.git(self.target, "log", "-1", "--format=%s"), "Finish the feature")
        self.assertEqual((self.target / "file.txt").read_text(), "unstaged after staging\n")
        self.assertEqual((self.target / "new file.txt").read_text(), "new\n")
        self.assertFalse((self.target / "ignored.txt").exists())

    def test_no_existing_feature_commit_can_commit_and_merge(self):
        self.git(self.source, "reset", "--hard", "main")
        (self.source / "new.txt").write_text("first change\n")
        self.assertEqual(self.inspect().commits_ahead, 0)
        self.execute(message="First feature commit")
        self.assertTrue(self.removed)
        self.assertTrue((self.target / "new.txt").exists())

    def test_clean_already_merged_worktree_needs_no_new_commit(self):
        self.git(self.source, "reset", "--hard", "main")
        before = self.git(self.target, "rev-parse", "HEAD")
        result = self.execute()
        self.assertNotIn(workflow.Phase.COMMIT, result.completed)
        self.assertTrue(self.removed)
        self.assertEqual(self.git(self.target, "rev-parse", "HEAD"), before)

    def test_dirty_worktree_requires_nonempty_message_without_staging(self):
        (self.source / "new.txt").write_text("new\n")
        before = self.git(self.source, "status", "--porcelain")
        for message in (None, "", "  ", "bad\0message"):
            with self.subTest(message=message), self.assertRaisesRegex(workflow.WorkflowError, "commit message"):
                self.execute(message=message)
            self.assertEqual(self.git(self.source, "status", "--porcelain"), before)
            self.assertFalse(self.removed)

    def test_edits_to_already_dirty_files_invalidate_confirmation(self):
        for filename in ("file.txt", "untracked.txt"):
            with self.subTest(filename=filename):
                (self.source / filename).write_text("first\n")
                plan = self.inspect()
                (self.source / filename).write_text("second\n")
                with self.assertRaisesRegex(workflow.WorkflowError, "changed since confirmation"):
                    self.execute(plan, message="Do not commit changed files")
        self.assertFalse(self.removed)

    def test_byte_changes_in_dirty_text_invalidate_confirmation(self):
        file = self.source / "file.txt"
        file.write_bytes(b"changed\n")
        plan = self.inspect()
        file.write_bytes(b"changed\r\n")
        with self.assertRaisesRegex(workflow.WorkflowError, "changed since confirmation"):
            self.execute(plan, message="Do not commit changed bytes")

    def test_untracked_whitespace_names_and_symlinks_are_committed(self):
        names = (" leading\nfile ", "has\rreturn.txt", "unicode-雪.txt")
        for name in names:
            (self.source / name).write_bytes(b"new\xff\r\n")
        (self.source / "link").symlink_to("file.txt")
        self.execute(message="Add unusual filenames")
        for name in names:
            self.assertEqual((self.target / name).read_bytes(), b"new\xff\r\n")
        self.assertTrue((self.target / "link").is_symlink())

    def test_non_utf8_filename_is_committed_when_filesystem_supports_it(self):
        name = os.fsdecode(b"non-utf8-\xff.txt")
        try:
            (self.source / name).write_bytes(b"new\xff\r\n")
        except OSError as error:
            # macOS filesystems can reject invalid UTF-8 path bytes. This is a
            # filesystem capability, not a reason to skip the portable cases.
            if error.errno == errno.EILSEQ:
                self.skipTest("Filesystem does not support non-UTF-8 filenames")
            raise
        self.execute(message="Preserve filename bytes")
        self.assertEqual((self.target / name).read_bytes(), b"new\xff\r\n")

    def test_non_utf8_tracked_contents_are_committed_exactly(self):
        (self.source / "file.txt").write_bytes(b"non-utf8 \xff\r\n")
        self.execute(message="Preserve bytes")
        self.assertEqual((self.target / "file.txt").read_bytes(), b"non-utf8 \xff\r\n")

    def test_checkout_root_with_trailing_newline_is_supported(self):
        renamed = self.root / "feature checkout\n"
        self.git(self.target, "worktree", "move", str(self.source), str(renamed))
        self.source = renamed
        self.selection = (self.source, self.target, True)
        self.assertEqual(self.inspect().source, renamed)
        self.execute()
        self.assertFalse(renamed.exists())

    def test_commit_failure_keeps_worktree_and_target_unchanged(self):
        hook = self.target / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\necho 'commit rejected' >&2\nexit 1\n")
        hook.chmod(0o755)
        before = self.git(self.target, "rev-parse", "HEAD")
        (self.source / "new.txt").write_text("new\n")
        with self.assertRaisesRegex(workflow.ExecutionFailure, "commit rejected") as caught:
            self.execute(message="Rejected")
        self.assertEqual(caught.exception.phase, workflow.Phase.COMMIT)
        self.assertIn(workflow.Phase.STAGE, caught.exception.result.completed)
        self.assertNotIn(workflow.Phase.COMMIT, caught.exception.result.completed)
        self.assertFalse(self.removed)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.git(self.target, "rev-parse", "HEAD"), before)
        self.assertIn("A  new.txt", self.git(self.source, "status", "--porcelain"))

    def test_hook_changes_after_commit_stop_before_merge_and_report_commit(self):
        hook = self.target / ".git/hooks/post-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook change' > file.txt\n")
        hook.chmod(0o755)
        (self.source / "new.txt").write_text("new\n")
        before = self.git(self.target, "rev-parse", "HEAD")
        with self.assertRaises(workflow.ExecutionFailure) as caught:
            self.execute(message="Keep this commit")
        self.assertIn(workflow.Phase.COMMIT, caught.exception.result.completed)
        self.assertEqual(caught.exception.result.committed_head, self.git(self.source, "rev-parse", "HEAD"))
        self.assertFalse(self.removed)
        self.assertEqual(self.git(self.target, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(self.source, "log", "-1", "--format=%s"), "Keep this commit")

    def test_conflict_after_commit_preserves_new_commit(self):
        self.commit(self.target, "main conflict", "main\n")
        (self.source / "file.txt").write_text("feature\n")
        before = self.git(self.target, "rev-parse", "HEAD")
        with self.assertRaises(workflow.ExecutionFailure) as caught:
            self.execute(message="Feature conflict")
        self.assertIn(workflow.Phase.COMMIT, caught.exception.result.completed)
        self.assertFalse(self.removed)
        self.assertEqual(self.git(self.target, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(self.target, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.source, "log", "-1", "--format=%s"), "Feature conflict")

    def test_ignored_target_file_is_not_overwritten(self):
        (self.root / "ignore").write_text("local.env\n")
        self.git(self.target, "config", "core.excludesFile", str(self.root / "ignore"))
        (self.target / "local.env").write_text("SECRET=keep-me\n")
        (self.source / "local.env").write_text("SECRET=example\n")
        self.git(self.source, "add", "--force", "local.env")
        self.git(self.source, "commit", "-m", "Track example config")
        before = self.git(self.target, "rev-parse", "HEAD")
        with self.assertRaises(workflow.ExecutionFailure) as caught:
            self.execute()
        self.assertEqual(caught.exception.phase, workflow.Phase.MERGE)
        self.assertEqual((self.target / "local.env").read_text(), "SECRET=keep-me\n")
        self.assertEqual(self.git(self.target, "rev-parse", "HEAD"), before)
        self.assertFalse(self.removed)

    def test_post_merge_hook_cannot_drop_target_history_and_delete_checkout(self):
        self.commit(self.target, "main only", "main\n", filename="main-only.txt")
        plan = self.inspect()
        hook = self.target / ".git/hooks/post-merge"
        hook.write_text(f"#!/bin/sh\ngit reset --hard {plan.source_head}\n")
        hook.chmod(0o755)
        with self.assertRaisesRegex(workflow.ExecutionFailure, "target history was not preserved") as caught:
            self.execute(plan)
        self.assertEqual(caught.exception.phase, workflow.Phase.VERIFY)
        self.assertIn(workflow.Phase.MERGE, caught.exception.result.completed)
        self.assertFalse(caught.exception.result.removal_attempted)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.removed)

    def test_branch_merge_options_are_not_inherited_or_silently_bypassed(self):
        self.commit(self.target, "main only", "main\n", filename="main-only.txt")
        before = self.git(self.target, "rev-parse", "HEAD")
        for option in ("--strategy=ours", "--squash", "--autostash", "--ff-only", "--verify-signatures"):
            with self.subTest(option=option):
                self.git(self.target, "config", "--", "branch.main.mergeOptions", option)
                with self.assertRaisesRegex(workflow.WorkflowError, "mergeOptions"):
                    self.inspect()
                self.assertEqual(self.git(self.target, "rev-parse", "HEAD"), before)
                self.assertFalse(self.removed)

    def test_fast_forward_only_policy_is_respected(self):
        self.commit(self.target, "diverge", "main\n", filename="main-only.txt")
        self.git(self.target, "config", "merge.ff", "only")
        with self.assertRaises(workflow.ExecutionFailure) as caught:
            self.execute()
        self.assertEqual(caught.exception.phase, workflow.Phase.MERGE)
        self.assertFalse(self.removed)

    def test_inherited_git_routing_and_index_are_ignored(self):
        index = self.root / "unrelated-index"
        with patch.dict(os.environ, {"GIT_DIR": "/missing/repo", "GIT_WORK_TREE": "/missing/tree",
                                     "GIT_INDEX_FILE": str(index), "GIT_CONFIG_COUNT": "1",
                                     "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/bad/hooks"}):
            self.execute()
        self.assertFalse(index.exists())
        self.assertTrue(self.removed)
        for _, options in self.runner.calls:
            self.assertNotIn("GIT_DIR", options["env"])
            self.assertNotIn("GIT_CONFIG_COUNT", options["env"])

    def test_commit_preview_is_bounded_and_uses_confirmed_heads(self):
        self.assertEqual(self.inspect().commits_ahead, 1)
        self.assertIn("feature", self.inspect().commits[0])
        self.git(self.target, "tag", "main", "feature")
        self.assertEqual(self.inspect().commits_ahead, 1)
        for index in range(23):
            self.git(self.source, "commit", "--allow-empty", "-m", f"Extra {index}")
        plan = self.inspect()
        self.assertEqual(plan.commits_ahead, 24)
        self.assertEqual(len(plan.commits), 20)

    def test_commit_signing_policy_is_not_bypassed(self):
        (self.source / "new.txt").write_text("new\n")
        self.git(self.source, "config", "commit.gpgsign", "true")
        self.git(self.source, "config", "gpg.program", str(self.root / "missing-gpg"))
        with self.assertRaises(workflow.ExecutionFailure) as caught:
            self.execute(message="Must sign")
        self.assertEqual(caught.exception.phase, workflow.Phase.COMMIT)
        self.assertFalse(self.removed)

    def test_merge_signing_policy_is_not_bypassed(self):
        self.commit(self.target, "main only", "main\n", filename="main-only.txt")
        self.git(self.target, "config", "commit.gpgsign", "true")
        self.git(self.target, "config", "gpg.program", str(self.root / "missing-gpg"))
        with self.assertRaises(workflow.ExecutionFailure) as caught:
            self.execute()
        self.assertEqual(caught.exception.phase, workflow.Phase.MERGE)
        self.assertFalse(self.removed)

    def test_rename_original_path_is_not_parsed_as_a_status_record(self):
        self.commit(self.source, "unusual path", "contents\n", filename="1 a S")
        self.git(self.source, "mv", "1 a S", "renamed.txt")
        self.execute(message="Rename unusual path")
        self.assertEqual((self.target / "renamed.txt").read_text(), "contents\n")
        self.assertFalse((self.target / "1 a S").exists())

    def test_malformed_reply_after_removal_reports_uncertain_outcome(self):
        original = self.herdr
        def malformed(*args):
            reply = original(*args)
            return b"not JSON" if args[:3] == ("fake-herdr", "worktree", "remove") else reply
        with patch.object(self, "herdr", side_effect=malformed):
            with self.assertRaises(workflow.ExecutionFailure) as caught:
                self.execute()
        self.assertFalse(self.source.exists())
        self.assertTrue(caught.exception.result.removal_attempted)
        self.assertFalse(caught.exception.result.removed)
        self.assertIn("Removal was attempted", caught.exception.result.recovery())

    def test_unexpected_adapter_failure_still_carries_execution_context(self):
        original = self.herdr
        def unexpected(*args):
            if args[:3] == ("fake-herdr", "worktree", "remove"):
                raise RuntimeError("unexpected transport failure")
            return original(*args)
        with patch.object(self, "herdr", side_effect=unexpected):
            with self.assertRaises(workflow.ExecutionFailure) as caught:
                self.execute()
        self.assertIsInstance(caught.exception.cause, RuntimeError)
        self.assertIn(workflow.Phase.MERGE, caught.exception.result.completed)
        self.assertTrue(self.source.exists())

    def test_removal_json_error_does_not_report_success(self):
        original = self.herdr
        def rejected(*args):
            if args[:3] == ("fake-herdr", "worktree", "remove"):
                return response({"error": {"code": "worktree_locked"}})
            return original(*args)
        with patch.object(self, "herdr", side_effect=rejected):
            with self.assertRaisesRegex(workflow.ExecutionFailure, "worktree_locked") as caught:
                self.execute()
        self.assertTrue(caught.exception.result.removal_attempted)
        self.assertFalse(caught.exception.result.removed)
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
