"""Headless tests of actual Textual widgets, keyboard flow, and recovery states."""

import asyncio
import threading
import unittest
from unittest.mock import patch

from textual.content import Content
from textual.widgets import Button, Input, Static

from commands import CommandError
from merge_delete_ui import AppState, MergeDeleteApp
from test_merge_delete import GitFixture
import worktree_workflow as workflow


class MergeDeleteUITests(GitFixture, unittest.IsolatedAsyncioTestCase):
    def app(self):
        return MergeDeleteApp(self.operation, self.workspace_id)

    async def loaded(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_clean_defaults_to_cancel_and_enter_changes_nothing(self):
        before = self.git(self.target, "rev-parse", "HEAD")
        app = self.app()
        async with app.run_test(size=(90, 32)) as pilot:
            await self.loaded(app, pilot)
            self.assertEqual(app.state, AppState.REVIEWING)
            self.assertEqual(app.focused.id, "cancel")
            self.assertFalse(app.query_one("#message").display)
            self.assertFalse(app.query_one("#confirm", Button).disabled)
            self.assertIn("feature", str(app.query_one("#commits", Static).content))
            await pilot.press("enter")
        self.assertEqual(app.return_value, 0)
        self.assertEqual(self.git(self.target, "rev-parse", "HEAD"), before)
        self.assertFalse(self.removed)

    async def test_arrows_navigate_clean_confirmation_in_both_directions(self):
        app = self.app()
        async with app.run_test(size=(72, 22)) as pilot:
            await self.loaded(app, pilot)
            self.assertTrue(app.current_theme.ansi)
            self.assertTrue(app.native_ansi_color)
            self.assertEqual(app.screen.styles.background.ansi, -1)
            self.assertEqual(app.screen.styles.color.ansi, -1)
            for span in Content.from_rich_text(app.query_one("#branches", Static).content).spans:
                if span.style.foreground is not None:
                    self.assertIsNotNone(span.style.foreground.ansi)
            self.assertEqual(app.focused.id, "cancel")
            for key, expected in (("right", "confirm"), ("left", "cancel"), ("up", "confirm"), ("down", "cancel")):
                await pilot.press(key)
                self.assertEqual(app.focused.id, expected)
            self.assertFalse(self.removed)
            await pilot.press("escape")

    async def test_arrows_leave_horizontal_cursor_editing_intact(self):
        (self.source / "new.txt").write_text("new\n")
        app = self.app()
        async with app.run_test(size=(72, 22)) as pilot:
            await self.loaded(app, pilot)
            message = app.query_one("#message", Input)
            message.value = "Finish feature"
            await pilot.pause()
            await pilot.press("home", "right")
            self.assertEqual(message.cursor_position, 1)
            self.assertEqual(app.focused.id, "message")
            await pilot.press("left")
            self.assertEqual(message.cursor_position, 0)
            for key, expected in (("down", "cancel"), ("right", "confirm"), ("left", "cancel"), ("up", "message")):
                await pilot.press(key)
                self.assertEqual(app.focused.id, expected)
            self.assertEqual(message.value, "Finish feature")
            self.assertFalse(self.removed)
            await pilot.press("escape")

    async def test_arrows_skip_disabled_confirmation(self):
        (self.source / "new.txt").write_text("new\n")
        app = self.app()
        async with app.run_test(size=(48, 18)) as pilot:
            await self.loaded(app, pilot)
            self.assertTrue(app.query_one("#confirm", Button).disabled)
            self.assertTrue(app.query_one("#confirm", Button).styles.text_style.dim)
            await pilot.press("down")
            self.assertEqual(app.focused.id, "cancel")
            await pilot.press("right")
            self.assertEqual(app.focused.id, "message")
            await pilot.press("escape")

    async def test_clean_keyboard_confirmation_merges_and_deletes(self):
        app = self.app()
        async with app.run_test(size=(90, 32)) as pilot:
            await self.loaded(app, pilot)
            await pilot.press("tab")
            self.assertEqual(app.focused.id, "confirm")
            await pilot.press("enter")
            await self.loaded(app, pilot)
            self.assertEqual(app.state, AppState.SUCCEEDED)
            self.assertTrue(app.finished)
            self.assertTrue(app.result.removed)
            self.assertTrue(self.removed)
            self.assertEqual(app.exit_code, 0)
            await pilot.press("escape")

    async def test_dirty_message_enter_only_focuses_action_then_confirms(self):
        (self.source / "new.txt").write_text("new\n")
        app = self.app()
        async with app.run_test(size=(90, 32)) as pilot:
            await self.loaded(app, pilot)
            self.assertEqual(app.focused.id, "message")
            confirm = app.query_one("#confirm", Button)
            self.assertGreaterEqual(confirm.content_region.width, len(str(confirm.label)))
            self.assertTrue(confirm.disabled)
            await pilot.press("space", "enter")
            self.assertTrue(confirm.disabled)
            app.query_one("#message", Input).value = "Finish feature"
            await pilot.pause()
            await pilot.press("enter")
            self.assertEqual(app.focused.id, "confirm")
            self.assertFalse(self.removed)
            self.assertEqual(self.git(self.source, "log", "-1", "--format=%s"), "feature")
            await pilot.press("enter")
            await self.loaded(app, pilot)
            self.assertTrue(self.removed)
            self.assertEqual(self.git(self.target, "log", "-1", "--format=%s"), "Finish feature")

    async def test_dirty_escape_does_not_stage_commit_or_merge(self):
        (self.source / "new.txt").write_text("new\n")
        before = self.git(self.source, "status", "--porcelain")
        app = self.app()
        async with app.run_test(size=(48, 18)) as pilot:
            await self.loaded(app, pilot)
            await pilot.press("escape")
        self.assertEqual(app.return_value, 0)
        self.assertEqual(self.git(self.source, "status", "--porcelain"), before)
        self.assertFalse(self.removed)

    async def test_missing_explicit_selection_never_uses_focused_workspace(self):
        app = MergeDeleteApp(self.operation, None)
        with patch.object(self.runner, "run") as run:
            async with app.run_test(size=(80, 28)) as pilot:
                await self.loaded(app, pilot)
                self.assertEqual(app.exit_code, 1)
                self.assertIn("right-click menu", str(app.query_one("#result", Static).content))
                run.assert_not_called()
                await pilot.press("enter")
        self.assertEqual(app.return_value, 1)

    async def test_failure_shows_recovery_and_close_button(self):
        (self.target / "untracked.txt").write_text("dirty target\n")
        app = self.app()
        async with app.run_test(size=(80, 28)) as pilot:
            await self.loaded(app, pilot)
            self.assertEqual(app.state, AppState.FAILED)
            self.assertTrue(app.query_one("#result").display)
            self.assertIn("No mutation was started", str(app.query_one("#result", Static).content))
            self.assertTrue(app.query_one("#confirm", Button).disabled)
            self.assertEqual(str(app.query_one("#cancel", Button).label), "Close")
            self.assertEqual(app.focused.id, "cancel")
            await pilot.press("escape")
        self.assertFalse(self.removed)
        self.assertEqual(app.return_value, 1)

    async def test_narrow_panel_keeps_actions_visible_and_keyboard_reachable(self):
        (self.source / "new.txt").write_text("new\n")
        app = self.app()
        async with app.run_test(size=(48, 18)) as pilot:
            await self.loaded(app, pilot)
            message = app.query_one("#message", Input)
            review = app.query_one("#review")
            self.assertGreaterEqual(message.region.y, review.region.bottom)
            self.assertLess(message.region.bottom, app.size.height)
            message.value = "Narrow panel"
            await pilot.pause()
            await pilot.press("enter")
            confirm = app.query_one("#confirm", Button)
            self.assertEqual(app.focused, confirm)
            self.assertLessEqual(confirm.region.bottom, app.size.height)
            self.assertGreaterEqual(confirm.region.x, 0)
            self.assertLessEqual(confirm.region.right, app.size.width)
            await pilot.press("shift+tab")
            self.assertEqual(app.focused.id, "cancel")
            await pilot.press("escape")

    async def test_small_panel_has_one_line_buttons_and_no_controls_legend(self):
        (self.source / "new.txt").write_text("new\n")
        app = self.app()
        # Herdr's 64x18 outer popup leaves a 61x16 terminal surface.
        async with app.run_test(size=(61, 16)) as pilot:
            await self.loaded(app, pilot)
            self.assertFalse(app.query("#keys"))
            self.assertFalse(app.query("#title"))
            self.assertEqual(app.query_one("#review").border_title, "Merge & delete")
            for name in ("cancel", "confirm"):
                button = app.query_one(f"#{name}", Button)
                self.assertEqual(button.region.height, 1)
                self.assertLessEqual(button.region.right, app.size.width)
                self.assertLessEqual(button.region.bottom, app.size.height)
            message = app.query_one("#message", Input)
            self.assertEqual(message.region.height, 3)
            self.assertGreaterEqual(message.region.y, 0)
            self.assertLess(message.region.bottom, app.size.height)
            await pilot.press("escape")

    async def test_many_files_scroll_without_hiding_form_or_actions(self):
        for index in range(40):
            (self.source / f"new-{index:02}.txt").write_text("new\n")
        app = self.app()
        async with app.run_test(size=(61, 16)) as pilot:
            await self.loaded(app, pilot)
            review = app.query_one("#review")
            message = app.query_one("#message", Input)
            buttons = app.query_one("#actions")
            regions = (message.region, buttons.region)
            await pilot.press("pagedown")
            self.assertGreater(review.scroll_y, 0)
            self.assertEqual((message.region, buttons.region), regions)
            self.assertEqual(app.focused.id, "message")
            await pilot.press("pageup")
            self.assertEqual(review.scroll_y, 0)
            self.assertFalse(self.removed)
            await pilot.press("escape")

    async def test_cancel_during_loading_discards_late_plan_without_mutation(self):
        plan = self.inspect()
        entered = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        def delayed(workspace_id):
            entered.set()
            release.wait(2)
            returned.set()
            return plan
        app = self.app()
        with patch.object(self.operation, "inspect", side_effect=delayed):
            try:
                async with app.run_test(size=(61, 16)) as pilot:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    self.assertEqual(app.state, AppState.LOADING)
                    await pilot.press("escape")
                    release.set()
                    self.assertTrue(await asyncio.to_thread(returned.wait, 1))
            finally:
                release.set()
        self.assertEqual(app.return_value, 0)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.removed)

    async def test_cancel_is_blocked_during_git_mutation(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.loaded(app, pilot)
            app.state = AppState.EXECUTING
            await pilot.press("escape", "ctrl+c", "ctrl+q")
            self.assertTrue(app.is_running)
            app.state = AppState.REVIEWING
            await pilot.press("escape")

    async def test_partial_success_keeps_paths_progress_and_commit_in_error(self):
        (self.source / "new.txt").write_text("new\n")
        original = self.herdr
        def rejected(*args):
            if args[:3] == ("fake-herdr", "worktree", "remove"):
                raise CommandError(args, stderr=b"cannot remove", returncode=1)
            return original(*args)
        app = self.app()
        with patch.object(self, "herdr", side_effect=rejected):
            async with app.run_test(size=(61, 16)) as pilot:
                await self.loaded(app, pilot)
                app.query_one("#message", Input).value = "Keep this commit"
                await pilot.pause()
                await pilot.press("enter", "enter")
                await self.loaded(app, pilot)
                self.assertEqual(app.state, AppState.FAILED)
                self.assertIn(workflow.Phase.COMMIT, app.result.completed)
                self.assertIn(workflow.Phase.MERGE, app.result.completed)
                self.assertTrue(app.query_one("#branches").display)
                self.assertTrue(app.query_one("#progress").display)
                content = str(app.query_one("#result", Static).content)
                self.assertIn(str(self.source), content)
                self.assertIn(str(self.target), content)
                self.assertIn(app.result.committed_head, content)
                self.assertIn("Removal was attempted", content)
                await pilot.press("pageup", "pagedown", "ctrl+pageup", "ctrl+pagedown", "escape")
        self.assertTrue(self.source.exists())
        self.assertEqual(app.return_value, 1)

    async def test_hook_output_is_escaped_and_streamed_into_progress(self):
        (self.source / "new.txt").write_text("new\n")
        hook = self.target / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf '\\033[31mhook output\\033[0m\\n' >&2\n")
        hook.chmod(0o755)
        app = self.app()
        async with app.run_test(size=(61, 16)) as pilot:
            await self.loaded(app, pilot)
            app.query_one("#message", Input).value = "Hook output"
            await pilot.pause()
            await pilot.press("enter", "enter")
            await self.loaded(app, pilot)
            self.assertEqual(app.state, AppState.SUCCEEDED)
            lines = "\n".join(line.text for line in app.query_one("#progress").lines)
            self.assertIn("\\x1b[31mhook output", lines)
            self.assertNotIn("\x1b", lines)
            await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
