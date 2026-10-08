# Architecture and development

This guide is for contributors and anyone who wants to understand the checks
behind the merge-and-close workflow. For installation and everyday use, start
with the [README](README.md).

## Run locally

From a checkout of this repository:

```sh
python3 -E -s install.py
.venv/bin/python3 -m unittest -v
herdr plugin link "$PWD"
```

`plugin link` does not run the installer. Rerun the installer after Python
upgrades if the virtual environment stops working. Avoid linking over a plugin
registration managed by another installation method.

To open the panel with an explicit workspace for testing:

```sh
herdr workspace list
herdr plugin pane open --plugin herdr.worktree-tools --entrypoint merge-delete \
  --workspace ws_ID --env HERDR_MERGE_WORKSPACE_ID=ws_ID
```

Replace both occurrences of `ws_ID` with the workspace you want to review.
Opening without an explicit selection stops safely.

## Where to make changes

| File | Responsibility |
| --- | --- |
| `merge_delete.py` | Read the selected workspace and compose the adapters, workflow, and panel. Load the UI only for popup execution. |
| `worktree_workflow.py` | Inspect a workspace, build the review plan, validate it, and execute commit, merge, and removal. |
| `herdr_client.py` | Validate Herdr responses and handle workspace lookup, popup creation, and non-forced removal. |
| `commands.py` | Run subprocesses, preserve bytes, stream diagnostics, escape display output, and apply Git environment policy. |
| `merge_delete_ui.py` | Present loading, review, execution, success, and failure states. Run work off the UI thread. |
| `herdr-plugin.toml` | Declare installation, the action, and the popup. |
| `install.py` / `launch.sh` | Install isolated dependencies and launch the plugin. |

The UI does not decide whether a merge or removal is safe. Callers use
`WorktreeWorkflow.inspect(workspace_id)` and
`execute(workspace_id, plan, message, report)`. The command runner and Herdr
adapter are injectable so tests can exercise real Git without a live Herdr
session.

## What a review authorizes

An immutable `Plan` binds checkout paths, branch identities, both HEADs, and a
SHA-256 fingerprint of source status, index, diffs, and untracked contents.
Fingerprint records preserve raw bytes and use length prefixes; NUL-separated
paths are not newline-normalized or replacement-decoded. Display text and the
list of up to 20 recent commit subjects are not confirmation authority.

Inspection requires a linked source worktree and a clean main-checkout target in
the same repository, with branch HEADs and no in-progress operation. It rejects
submodules, locked source worktrees, and nonempty target
`branch.<name>.mergeOptions`. Ordinary Git fast-forward and signing policies
remain effective.

## Execution sequence

1. Revalidate the selected workspace and reviewed plan before mutation.
2. For a dirty source, stage all non-ignored changes and commit with the user’s
   message. Check that the source is clean, on the reviewed branch, and has the
   reviewed source commit as its parent; verify that the target selection and HEAD
   have not changed.
3. Preflight conflicts with `merge-tree --write-tree`, then revalidate before
   merging. Use `--no-autostash` and `--no-overwrite-ignore` to protect target files.
4. Before removal, verify clean checkouts, matching branches and paths, and that
   both reviewed histories are ancestors of the current target HEAD. Recheck the
   Herdr selection, then request non-forced worktree removal.

The plugin never deletes branches, pushes, forces cleanup, recursively deletes
checkout directories itself, or resets successful work. These checks are not an
atomic transaction across Git, the filesystem, hooks, and Herdr. Concurrent
writers can still race a check.

## Subprocess behavior

Arguments are arrays, not shell-interpolated strings. Git routing, index/object
store overrides, and environment-injected command options are removed for both
Git and Herdr. Explicit config-file selection and Herdr session/socket context
are retained. Git runs without a pager, replacement objects, external fsmonitor,
or terminal credential prompts. The launcher ignores Python import-path and
user-site overrides.

Children have closed stdin and no controlling terminal. Interactive terminal
hooks are unsupported; signing settings remain active. Trusted programs may still
open explicit terminal paths or external dialogs. Repositories, dependencies,
hooks, filters, and merge drivers are not sandboxed.

Read-only commands have a 30-second deadline. Mutating commands, including the
conflict preflight, have no automatic timeout. Both output pipes are drained,
with a bounded 1 MiB diagnostic tail per stream. Terminal controls, bidi controls,
and surrogate bytes are escaped for display. The UI keeps up to 500 rendered log
lines and shows periodic progress notices. A failed display callback must not
kill a running Git operation.

## Preserve recovery information

Execution returns an `ExecutionResult` or raises an `ExecutionFailure`. Both
retain recovery context: reviewed commits, completed phases, new commit or merge
HEADs when available, and whether removal was attempted. Failure also preserves
checkout paths and the original cause or command details.

Keep preservation checks separate from command success: a successful command
with a hook does not prove that all invariants still hold. Never automatically
retry a mutation or roll back completed work. A missing or malformed removal
response can follow successful deletion; report that outcome as uncertain.

## Tests and dependency updates

```sh
.venv/bin/python3 -m unittest -v
.venv/bin/python3 -m pip check
git diff --check
```

Tests use disposable Git repositories and a fake Herdr adapter. They cover exact
bytes, subprocess output and deadlines, closed stdin, detached sessions, escaped
diagnostics, workflow preservation, and headless Textual keyboard and recovery
flows. They do not merge or delete user worktrees.

`requirements.txt` declares the direct dependency; `requirements.lock` pins the
full runtime with universal-wheel SHA-256 hashes. The installer requires hashes
and wheels, not source builds. CI actions are pinned to commit IDs. These checks
constrain downloaded artifacts, but do not sandbox dependencies or verify every
pre-existing installed file.

CI is not a live Herdr smoke test. See [Publishing and releases](PUBLISHING.md)
for the release checklist and supported-platform checks.
