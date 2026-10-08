"""Plan and execute a commit-preserving, non-forced worktree removal.

The interface is a selected workspace, an immutable confirmation snapshot, and
an execution result (or failure carrying the same recovery context).
"""

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import os
from pathlib import Path
import stat

from commands import CommandError, Git, Reporter, display_path, display_text
from herdr_client import HerdrClient, HerdrError


class WorkflowError(Exception):
    pass


class Phase(Enum):
    VALIDATE = "confirmation validation"
    STAGE = "staging"
    COMMIT = "commit"
    PREFLIGHT = "conflict preflight"
    MERGE = "merge"
    VERIFY = "preservation checks"
    REMOVE = "worktree removal"


@dataclass(frozen=True)
class Plan:
    source: Path
    target: Path
    source_branch: str
    target_branch: str
    source_head: str
    target_head: str
    source_state: str
    # Presentation is not part of confirmation authority.
    changes: str = field(compare=False)
    commits_ahead: int = field(compare=False)
    commits: tuple[str, ...] = field(compare=False)


@dataclass(frozen=True)
class ExecutionResult:
    source: Path
    target: Path
    confirmed_source_head: str
    confirmed_target_head: str
    completed: tuple[Phase, ...] = ()
    committed_head: str | None = None
    merged_head: str | None = None
    removal_attempted: bool = False

    @property
    def removed(self) -> bool:
        return Phase.REMOVE in self.completed

    def recovery(self) -> str:
        lines = [f"from {display_path(self.source)}", f"into {display_path(self.target)}",
                 f"Reviewed source: {self.confirmed_source_head}",
                 f"Reviewed target: {self.confirmed_target_head}"]
        if Phase.COMMIT in self.completed:
            lines.append(f"Commit completed: {self.committed_head or 'inspect source HEAD'}")
        if Phase.MERGE in self.completed:
            lines.append(f"Merge command completed: {self.merged_head or 'inspect target HEAD'}")
        if self.removed:
            lines.append("Checkout and space removed; branch retained.")
        elif self.removal_attempted:
            lines.append("Removal was attempted; inspect Git worktrees and Herdr before retrying.")
        else:
            lines.append("No removal was attempted. The worktree has been kept.")
        lines.append("Successful changes are not rolled back. Inspect Git status in both checkouts before retrying.")
        return "\n".join(lines)


class ExecutionFailure(WorkflowError):
    def __init__(self, phase: Phase, result: ExecutionResult, cause: Exception):
        self.phase = phase
        self.result = result
        self.cause = cause
        self.command = cause.argv if isinstance(cause, CommandError) else None
        super().__init__(f"Stopped during {phase.value}: {display_text(str(cause))}")


class WorktreeWorkflow:
    def __init__(self, herdr: HerdrClient):
        self.herdr = herdr
        self.git = Git(herdr.runner)

    def inspect(self, workspace_id: str | None) -> Plan:
        try:
            return self._inspect(*self.herdr.workspace(workspace_id))
        except (CommandError, HerdrError, OSError, ValueError) as error:
            raise WorkflowError(display_text(str(error))) from error

    def _no_operation(self, path: Path) -> None:
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
            marker_path = self.git.path(path, "rev-parse", "--git-path", marker)
            if not marker_path.is_absolute():
                marker_path = path / marker_path
            if marker_path.exists():
                raise WorkflowError(f"Finish or abort the existing Git operation in {display_path(path)} first.")

    def _status(self, path: Path) -> str:
        return self.git.text(path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")

    def _clean_checkout(self, path: Path) -> None:
        self._no_operation(path)
        if self._status(path):
            raise WorkflowError(f"Commit or stash changes (including untracked files) in {display_path(path)} first.")

    def _source_state(self, path: Path) -> str:
        """Hash exact bytes and use unambiguous, length-prefixed records."""
        digest = hashlib.sha256()

        def record(value: bytes) -> None:
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)

        for args in (("status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=none"),
                     ("diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", "--ignore-submodules=none"),
                     ("diff", "--cached", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", "--ignore-submodules=none")):
            value = self.git.raw(path, *args)
            if args[0] == "status":
                entries = iter(value.split(b"\0"))
                for entry in entries:
                    fields = entry.split(b" ")
                    if fields[0] == b"u":
                        raise WorkflowError("Resolve unmerged files in the source checkout first.")
                    if fields[0] in (b"1", b"2"):
                        if fields[2].startswith(b"S") and fields[2][2:] != b"..":
                            raise WorkflowError("Commit or stash changes inside submodules first; they cannot be committed here.")
                        if fields[0] == b"2":
                            # A rename's original path is a separate NUL record, not status.
                            next(entries)
            record(value)
        names = self.git.raw(path, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        for name in filter(None, names):
            file = path / os.fsdecode(name)
            info = file.lstat()
            record(name)
            record(str(info.st_mode).encode("ascii"))
            if stat.S_ISLNK(info.st_mode):
                record(os.fsencode(os.readlink(file)))
            elif stat.S_ISREG(info.st_mode):
                # Refuse a concurrent symlink/FIFO replacement rather than following it.
                fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if (before.st_dev, before.st_ino, before.st_mode) != (info.st_dev, info.st_ino, info.st_mode):
                        raise WorkflowError("A file changed while inspecting the worktree. Reopen and review it again.")
                    digest.update(before.st_size.to_bytes(8, "big"))
                    size = 0
                    for chunk in iter(lambda: stream.read(65536), b""):
                        digest.update(chunk)
                        size += len(chunk)
                    after = os.fstat(stream.fileno())
                    if (size != before.st_size or before.st_mtime_ns != after.st_mtime_ns
                            or before.st_ctime_ns != after.st_ctime_ns):
                        raise WorkflowError("A file changed while inspecting the worktree. Reopen and review it again.")
            else:
                raise WorkflowError(f"Cannot commit this non-regular file: {display_path(file)}")
        return digest.hexdigest()

    def _removable(self, source: Path) -> None:
        git_dir = self.git.path(source, "rev-parse", "--absolute-git-dir")
        if (git_dir / "locked").exists():
            raise WorkflowError("The selected worktree is locked. Review its lock and unlock it manually before using this action.")
        entries = self.git.raw(source, "ls-files", "--stage", "-z").split(b"\0")
        if any(entry.startswith(b"160000 ") for entry in entries):
            # Conservatively reject gitlinks, including uninitialized submodules.
            # Never try to make Git's submodule-removal refusal pass with --force.
            raise WorkflowError("Worktrees containing submodules are not supported by this no-force removal action. Use Git/Herdr's manual workflow instead.")

    def _inspect(self, source: Path, target: Path) -> Plan:
        source, target = source.resolve(), target.resolve()
        if source == target:
            raise WorkflowError("The main checkout cannot be merged and deleted.")
        for path in (source, target):
            if self.git.path(path, "rev-parse", "--show-toplevel").resolve() != path:
                raise WorkflowError(f"Not a checkout root: {display_path(path)}")
            self._no_operation(path)
        self._clean_checkout(target)
        source_common = self.git.path(source, "rev-parse", "--path-format=absolute", "--git-common-dir").resolve()
        target_common = self.git.path(target, "rev-parse", "--path-format=absolute", "--git-common-dir").resolve()
        if source_common != target_common:
            raise WorkflowError("The selected worktree and main checkout belong to different repositories.")
        if self.git.path(source, "rev-parse", "--absolute-git-dir").resolve() == source_common:
            raise WorkflowError("The selected checkout is not a linked worktree.")
        if self.git.path(target, "rev-parse", "--absolute-git-dir").resolve() != target_common:
            raise WorkflowError("The merge target must be the main checkout.")
        try:
            # Explicit refs for identity; never resolve a branch via an ambiguous tag.
            source_ref = self.git.text(source, "symbolic-ref", "HEAD")
            target_ref = self.git.text(target, "symbolic-ref", "HEAD")
        except CommandError as error:
            raise WorkflowError("Both checkouts must be on branches, not detached HEADs.") from error
        if not source_ref.startswith("refs/heads/") or not target_ref.startswith("refs/heads/"):
            raise WorkflowError("Both checkouts must be on local branches, not symbolic tag or remote refs.")
        target_branch = target_ref.removeprefix("refs/heads/")
        try:
            merge_options = self.git.raw(target, "config", "--get", f"branch.{target_branch}.mergeOptions")
        except CommandError as error:
            if error.returncode != 1:
                raise
            merge_options = b""
        if merge_options.strip():
            raise WorkflowError("This action does not support branch-specific mergeOptions. Review that configuration and merge manually; its policies will not be silently bypassed.")
        source_head = self.git.text(source, "rev-parse", "HEAD")
        target_head = self.git.text(target, "rev-parse", "HEAD")
        changes = self._status(source)
        state = self._source_state(source)
        self._removable(source)
        commits_ahead = int(self.git.text(source, "rev-list", "--count", f"{target_head}..{source_head}"))
        preview = self.git.raw(source, "log", "--no-decorate", "--format=%h %s", "--max-count=20",
                               f"{target_head}..{source_head}")
        commits = tuple(display_text(line.decode("utf-8", errors="replace"))
                        for line in preview.removesuffix(b"\n").split(b"\n")) if preview else ()
        return Plan(source, target, source_ref.removeprefix("refs/heads/"), target_ref.removeprefix("refs/heads/"),
                    source_head, target_head, state, changes, commits_ahead, commits)

    def _validate(self, workspace_id: str, plan: Plan) -> None:
        if self.inspect(workspace_id) != plan:
            raise WorkflowError("The checkouts changed since confirmation. Reopen the action and review them again.")

    def execute(self, workspace_id: str, plan: Plan, message: str | None = None,
                report: Reporter = print) -> ExecutionResult:
        result = ExecutionResult(plan.source, plan.target, plan.source_head, plan.target_head)
        phase = Phase.VALIDATE

        def announce(text: str) -> None:
            try:
                report(display_text(text))
            except Exception:
                # Reporting cannot be allowed to strand an otherwise valid mutation.
                pass

        def completed(step: Phase, **updates) -> None:
            nonlocal result
            result = replace(result, completed=(*result.completed, step), **updates)

        try:
            self._validate(workspace_id, plan)
            completed(phase)
            if plan.changes:
                if not message or not message.strip() or "\0" in message:
                    raise WorkflowError("Write a nonempty commit message without NUL characters before committing these changes.")
                phase = Phase.STAGE
                announce("Staging all non-ignored changes…")
                self.git.raw(plan.source, "add", "--all", mutating=True, report=announce)
                completed(phase)
                phase = Phase.COMMIT
                announce("Committing staged, unstaged, and untracked changes…")
                self.git.raw(plan.source, "commit", "-m", message.strip(), mutating=True, report=announce)
                completed(phase)
                result = replace(result, committed_head=self.git.text(plan.source, "rev-parse", "HEAD"))
                phase = Phase.VERIFY
                self._clean_checkout(plan.source)
                committed = self.inspect(workspace_id)
                if (committed.source != plan.source or committed.target != plan.target
                        or committed.source_branch != plan.source_branch
                        or committed.target_branch != plan.target_branch or committed.target_head != plan.target_head
                        or self.git.text(plan.source, "rev-parse", "HEAD^") != plan.source_head):
                    raise WorkflowError("A checkout changed during the commit. The worktree has been kept.")
                plan = committed
                self._validate(workspace_id, plan)
            phase = Phase.PREFLIGHT
            announce("Checking for merge conflicts…")
            # merge-tree writes objects and can invoke configured merge drivers; do not kill it on a deadline.
            self.git.raw(plan.target, "merge-tree", "--write-tree", plan.target_head, plan.source_head,
                         mutating=True, report=announce)
            completed(phase)
            self._validate(workspace_id, plan)
            phase = Phase.MERGE
            announce(f"Merging {plan.source_branch} into {plan.target_branch}…")
            # Nonempty branch.mergeOptions was rejected during inspection. Keep
            # it empty here too, in case configuration changes after validation.
            # Normal Git fast-forward and signing policies still apply.
            self.git.raw(plan.target, "-c", f"branch.{plan.target_branch}.mergeOptions=",
                         "-c", "merge.autoStash=false", "merge", "--no-edit", "--no-autostash",
                         "--no-overwrite-ignore", plan.source_head, mutating=True, report=announce)
            completed(phase)
            result = replace(result, merged_head=self.git.text(plan.target, "rev-parse", "HEAD"))
            phase = Phase.VERIFY
            self._clean_checkout(plan.source)
            current = self.inspect(workspace_id)
            if (current.source != plan.source or current.target != plan.target
                    or current.source_head != plan.source_head or current.source_branch != plan.source_branch
                    or current.target_branch != plan.target_branch):
                raise WorkflowError("A checkout changed during the merge. The worktree has been kept.")
            for label, head in (("source", plan.source_head), ("target", plan.target_head)):
                try:
                    self.git.raw(plan.target, "merge-base", "--is-ancestor", head, current.target_head)
                except CommandError as error:
                    if error.returncode != 1:
                        raise
                    raise WorkflowError(f"The reviewed {label} history was not preserved by the merge. The worktree has been kept.") from error
            if self.herdr.workspace(workspace_id) != (plan.source, plan.target):
                raise WorkflowError("The selected space changed during the merge. No checkout was deleted.")
            completed(phase)
            phase = Phase.REMOVE
            announce("Merge verified. Removing the worktree checkout…")
            result = replace(result, removal_attempted=True)
            self.herdr.remove(workspace_id, plan.source, announce)
            completed(phase)
        except Exception as error:
            # Even unexpected adapter failures must retain partial-success context.
            # Do not catch process interrupts or attempt automatic rollback.
            raise ExecutionFailure(phase, result, error) from error
        announce(f"Done. Branch {plan.source_branch} is retained; nothing was pushed.")
        return result
