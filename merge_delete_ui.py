"""Compact, panel-based TUI; Git work stays off the UI thread."""

from enum import Enum

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, RichLog, Static

from commands import display_path, display_text
import worktree_workflow as workflow


class AppState(Enum):
    LOADING = "loading"
    REVIEWING = "reviewing"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MergeDeleteApp(App):
    TITLE = "Merge & delete worktree"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", priority=True),
        Binding("ctrl+q", "cancel", "Cancel", priority=True),
        Binding("pageup", "review_up", show=False, priority=True),
        Binding("pagedown", "review_down", show=False, priority=True),
        Binding("ctrl+pageup", "log_up", show=False, priority=True),
        Binding("ctrl+pagedown", "log_down", show=False, priority=True),
        Binding("up", "focus_previous", show=False, priority=True),
        Binding("down", "focus_next", show=False, priority=True),
        Binding("left", "focus_horizontal(-1)", show=False, priority=True),
        Binding("right", "focus_horizontal(1)", show=False, priority=True),
    ]
    CSS = """
    Screen { background: $background; }
    #review {
        height: 1fr;
        min-height: 4;
        border: round $secondary;
        border-title-color: $secondary;
        border-title-style: bold;
        padding: 0 1;
        scrollbar-size: 1 1;
    }
    #branches, #summary, #changes, #commits, #result { height: auto; }
    #branches { margin-bottom: 1; }
    #summary { color: $primary; text-style: bold; }
    #changes { color: $text; }
    #notice { height: auto; color: $text-muted; }
    #message {
        height: 3;
        border: round $secondary;
        border-title-color: $secondary;
        background: $background;
    }
    #message:focus { border: round $primary; border-title-color: $primary; }
    #result { color: $text; }
    #progress { height: 8; background: $background; }
    #actions { height: 1; align-horizontal: right; }
    #actions Button {
        border: none !important;
        height: 1;
        width: auto;
        min-width: 0;
        margin-left: 1;
        background: $surface;
        color: $text;
        text-style: bold;
    }
    #actions #confirm { color: $success; }
    #actions Button:hover, #actions Button:focus {
        background: $surface;
        background-tint: transparent;
        color: $text;
        text-style: bold reverse;
    }
    #actions Button:disabled, #actions #confirm:disabled {
        color: $text-muted;
        text-style: dim;
    }
    """

    def __init__(self, operation: workflow.WorktreeWorkflow, workspace_id: str | None):
        super().__init__()
        # Emit native ANSI colors/defaults, not Textual's simulated RGB palette.
        self.theme = "ansi-dark"
        self.operation = operation
        self.workspace_id = workspace_id
        self.plan: workflow.Plan | None = None
        self.result: workflow.ExecutionResult | None = None
        self.state = AppState.LOADING

    @property
    def busy(self) -> bool:
        return self.state is AppState.EXECUTING

    @property
    def finished(self) -> bool:
        return self.state in (AppState.SUCCEEDED, AppState.FAILED)

    @property
    def exit_code(self) -> int:
        return int(self.state is AppState.FAILED)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="review", can_focus=False):
            yield Static("Reading the selected worktree…", id="branches", markup=False)
            yield Static("", id="summary", markup=False)
            yield Static("", id="changes", markup=False)
            yield Static("", id="commits", markup=False)
            yield Static("", id="result", markup=False)
            yield RichLog(id="progress", wrap=True, markup=False, auto_scroll=True, max_lines=500)
        # The form and actions stay visible even when the review has many files.
        yield Input(placeholder="Describe the changes", id="message")
        yield Static(
            "Deletes checkout, space + ignored files. No push.\n"
            "Keeps branch. Stop anything still writing to this worktree.",
            id="notice", markup=False,
        )
        with Horizontal(id="actions"):
            yield Button("Cancel", id="cancel", compact=True)
            yield Button("Merge & delete", id="confirm", compact=True, disabled=True)

    def on_mount(self):
        review = self.query_one("#review", VerticalScroll)
        review.border_title = "Merge & delete"
        message = self.query_one("#message", Input)
        message.border_title = "Commit message"
        message.border_subtitle = "All non-ignored changes · hooks run"
        self.query_one("#progress", RichLog).can_focus = False
        for name in ("summary", "changes", "commits", "message", "result", "progress"):
            self.query_one(f"#{name}").display = False
        self.query_one("#cancel", Button).focus()
        self.load_plan()

    def deliver(self, callback, *args):
        # Canceling a read-only load may close the app before its thread returns.
        if not self.is_running:
            return
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:
            if self.is_running:
                raise

    @work(thread=True)
    def load_plan(self):
        try:
            if not self.workspace_id:
                raise workflow.WorkflowError("Open this panel using the plugin action or a worktree's right-click menu.")
            plan = self.operation.inspect(self.workspace_id)
        except workflow.WorkflowError as error:
            self.deliver(self.show_error, error)
        else:
            self.deliver(self.show_plan, plan)

    def show_plan(self, plan: workflow.Plan):
        self.plan = plan
        self.state = AppState.REVIEWING
        routes = Text()
        routes.append(display_text(plan.source_branch), "bold magenta")
        routes.append("  →  ", "dim")
        routes.append(display_text(plan.target_branch), "bold green")
        routes.append(f"\nfrom {display_path(plan.source)}\ninto {display_path(plan.target)}", "dim")
        self.query_one("#branches", Static).update(routes)
        self.query_one("#review", VerticalScroll).border_subtitle = display_path(plan.target).split("/")[-1]
        summary = f"{plan.commits_ahead} commit(s)"
        changes = Text()
        if plan.changes:
            lines = plan.changes.splitlines()
            staged = sum(line[:1] not in (" ", "?") for line in lines)
            unstaged = sum(line[1:2] not in (" ", "?") for line in lines)
            untracked = sum(line.startswith("??") for line in lines)
            summary += f" · {staged} staged · {unstaged} unstaged · {untracked} new"
            for index, line in enumerate(lines):
                if index:
                    changes.append("\n")
                changes.append(line[:2], "yellow" if line.startswith("??") else "green")
                changes.append(display_text(line[2:]))
        else:
            summary += " · clean"
            changes.append("No new commit needed." if plan.commits_ahead else "Already merged. Remove checkout and space only.", "dim")
        self.query_one("#summary", Static).update(summary)
        self.query_one("#changes", Static).update(changes)
        commits = Text("Commits: ", style="dim")
        commits.append("\n".join(plan.commits))
        if plan.commits_ahead > len(plan.commits):
            commits.append(f"\n… {plan.commits_ahead - len(plan.commits)} more; inspect Git log for the full list.")
        self.query_one("#commits", Static).update(commits)
        self.query_one("#commits").display = bool(plan.commits)
        self.query_one("#summary").display = True
        self.query_one("#changes").display = True
        self.query_one("#message").display = bool(plan.changes)
        confirm = self.query_one("#confirm", Button)
        confirm.label = "Commit, merge & delete" if plan.changes else "Merge & delete"
        confirm.refresh(layout=True)
        confirm.disabled = bool(plan.changes)
        # A clean checkout defaults to Cancel; Enter must not delete it accidentally.
        if plan.changes:
            self.call_after_refresh(self.focus_message)

    def focus_message(self):
        self.query_one("#message", Input).focus()

    def check_action(self, action, parameters):
        # Horizontal arrows edit text inside the input, navigate everywhere else.
        if action == "focus_horizontal" and isinstance(self.focused, Input):
            return False
        return super().check_action(action, parameters)

    def action_focus_horizontal(self, direction):
        if direction < 0:
            self.action_focus_previous()
        else:
            self.action_focus_next()

    def action_review_up(self):
        self.query_one("#review", VerticalScroll).scroll_page_up(animate=False)

    def action_review_down(self):
        self.query_one("#review", VerticalScroll).scroll_page_down(animate=False)

    def action_log_up(self):
        self.query_one("#progress", RichLog).scroll_page_up(animate=False)

    def action_log_down(self):
        self.query_one("#progress", RichLog).scroll_page_down(animate=False)

    def on_input_changed(self, event: Input.Changed):
        if self.plan and self.state is AppState.REVIEWING:
            self.query_one("#confirm", Button).disabled = bool(self.plan.changes) and not event.value.strip()

    def on_input_submitted(self, event: Input.Submitted):
        # Enter in the message field advances to the explicit action, not deletion.
        if self.state is AppState.REVIEWING and event.value.strip():
            self.query_one("#confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "confirm" and self.plan and self.state is AppState.REVIEWING:
            message = self.query_one("#message", Input).value.strip()
            if self.plan.changes and not message:
                self.focus_message()
                return
            self.state = AppState.EXECUTING
            self.query_one("#confirm", Button).disabled = True
            self.query_one("#cancel", Button).disabled = True
            self.query_one("#message", Input).disabled = True
            for name in ("summary", "changes", "commits", "message"):
                self.query_one(f"#{name}").display = False
            self.query_one("#review", VerticalScroll).border_title = "Working…"
            self.query_one("#progress").display = True
            self.query_one("#notice", Static).update("Git is running. Keep this panel open until it finishes.")
            self.perform_merge(message)

    @work(thread=True)
    def perform_merge(self, message):
        def report(text):
            self.deliver(self.log_progress, text)
        try:
            result = self.operation.execute(self.workspace_id, self.plan, message, report)
        except workflow.ExecutionFailure as error:
            self.deliver(self.show_error, error)
        else:
            self.deliver(self.show_done, result)

    def log_progress(self, text):
        self.query_one("#progress", RichLog).write(display_text(text))

    def show_error(self, error: Exception):
        self.finish(1)
        review = self.query_one("#review", VerticalScroll)
        review.border_title = "Stopped"
        review.styles.border = ("round", "ansi_red")
        review.styles.border_title_color = "ansi_red"
        for name in ("summary", "changes", "commits"):
            self.query_one(f"#{name}").display = False
        if isinstance(error, workflow.ExecutionFailure):
            self.result = error.result
            recovery = error.result.recovery()
            notice = ("Removal outcome needs checking. No automatic rollback."
                      if error.result.removal_attempted else "Worktree kept. No automatic rollback.")
        else:
            recovery = "No mutation was started. Fix the reported problem, then reopen this action."
            notice = "Stopped before execution."
        result = self.query_one("#result", Static)
        result.update(f"{display_text(str(error))}\n\n{recovery}")
        result.display = True
        self.query_one("#notice", Static).update(notice)
        self.call_after_refresh(result.scroll_visible, animate=False)

    def show_done(self, result: workflow.ExecutionResult):
        self.result = result
        self.finish(0)
        review = self.query_one("#review", VerticalScroll)
        review.border_title = "Merged & removed"
        review.styles.border = ("round", "ansi_green")
        review.styles.border_title_color = "ansi_green"
        self.query_one("#notice", Static).update("Branch retained. Nothing pushed.")

    def finish(self, code):
        self.state = AppState.FAILED if code else AppState.SUCCEEDED
        self.query_one("#confirm", Button).disabled = True
        self.query_one("#confirm").display = False
        self.query_one("#message", Input).disabled = True
        self.query_one("#message").display = False
        cancel = self.query_one("#cancel", Button)
        cancel.disabled = False
        cancel.label = "Close"
        cancel.refresh(layout=True)
        cancel.focus()

    def action_cancel(self):
        # Git/hook processes cannot be safely canceled midway through mutation.
        if not self.busy:
            self.exit(self.exit_code)
