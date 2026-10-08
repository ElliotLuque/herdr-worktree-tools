# Worktree Tools for Herdr

Finish a Git worktree without juggling terminals: review your changes, commit
unfinished work, merge into your main checkout, and close the worktree’s Herdr
space from one keyboard-first panel.

The source branch is kept, and nothing is pushed. The panel uses your terminal’s
colors.

## Install

You need:

- **Herdr 0.9.3+** on Linux or macOS.
- **Git 2.38+**.
- **Python 3.11+**, with `venv` and pip support.
- Network access during installation to download Python dependencies.

```sh
herdr plugin install ElliotLuque/herdr-worktree-tools
```

Herdr previews the installation commands before running them. Dependencies are
installed in the plugin’s own `.venv`, not globally. Installation does not modify
Herdr’s configuration. Windows is not supported.

To update, run the same install command again. To install a specific release:

```sh
herdr plugin install ElliotLuque/herdr-worktree-tools --ref v0.3.0
```

The plugin ID is `herdr.worktree-tools`. If you manage the plugin through a local
link, update that checkout instead of installing a second copy.

## Merge and close a worktree

1. Stop any process or agent still writing to the worktree.
2. Make sure your main checkout has no uncommitted changes.
3. Focus the linked worktree’s space in Herdr and run:

   ```sh
   herdr plugin action invoke herdr.worktree-tools.merge-delete
   ```

4. Review the source branch, target branch, changed files, and recent commits.
5. If there are uncommitted changes, enter a commit message. Clean worktrees skip
   the commit form.
6. Confirm. The plugin commits if needed, merges, removes the worktree checkout,
   and closes its Herdr space.

**The merge target is the branch currently checked out in your main checkout.**
It is not necessarily `main` or your remote’s default branch. Check the route in
the panel before confirming.

### Add a keyboard shortcut

Choose an unused key in your Herdr configuration:

```toml
[[keys.command]]
key = "prefix+m"
type = "plugin_action"
command = "herdr.worktree-tools.merge-delete"
description = "Merge & delete worktree"
```

The action works with stock Herdr; no patched binary is required. Right-click
menu integration is not included in this plugin.

### Panel controls

| Key | Action |
| --- | --- |
| Arrow keys / Tab | Navigate |
| Enter | Activate the focused button |
| Esc | Cancel before execution |
| Left / Right in the message field | Move the text cursor |
| Page Up / Page Down | Scroll the review |
| Ctrl+Page Up / Ctrl+Page Down | Scroll the execution log |

Once execution starts, cancellation is disabled. Keep the panel open until it
finishes.

## What confirmation changes

- **All non-ignored changes are committed**, including untracked files and the
  unstaged portions of partially staged files. Exclude anything you do not want
  committed before opening the action.
- Existing commits are merged into the main checkout. Conflicts are checked
  before the actual merge, and ignored files in the target are protected from
  overwrite.
- **Removing the source checkout also deletes its ignored files**, such as local
  configuration and build output. Copy anything you need to keep elsewhere first.
- The source branch remains available. Nothing is pushed, and neither merging
  nor removal is forced.

Canceling before confirmation makes no changes. The plugin rejects main checkouts,
non-worktree spaces, locked worktrees, worktrees containing submodules (including
uninitialized ones), and targets with nonempty `branch.<name>.mergeOptions`.
Use Git and Herdr manually for those cases.

## Troubleshooting

### The panel does not open

Confirm that you selected a linked worktree, not the main checkout. Inspect the
plugin log for the error:

```sh
herdr plugin log list --plugin herdr.worktree-tools
```

The action uses the workspace you invoked it from, not the terminal’s current
directory. It will not guess a workspace if the selection is missing.

### The merge cannot proceed

Read the panel’s error and check both checkouts. Common causes are a dirty main
checkout, conflicting changes, an in-progress Git operation, or files changed
since you opened the review. Resolve the cause, then reopen the action to review
an up-to-date plan.

### A commit, hook, or signing step fails or takes a long time

Git hooks, merge drivers, and signing settings remain active. Commands cannot
accept terminal input: prepare a signing agent or graphical pinentry beforehand,
and use the manual workflow for hooks that require an interactive terminal.

The execution log streams diagnostics and shows periodic notices for slow
commands. Read-only checks have a 30-second deadline; commits, merges, conflict
preflights, and removal are not automatically killed on a deadline.

### A later step fails after a commit or merge succeeds

**Successful steps are not rolled back.** A failed commit can leave files staged;
a failed merge can leave a merge in progress in the main checkout. The panel
retains completed steps, checkout paths, reviewed commit IDs, and the execution
log. Inspect `git status` in both checkouts and fix or abort the reported operation
before retrying. Do not reset files just to dismiss the error.

If the removal result is uncertain, inspect `git worktree list` and Herdr’s
workspace list before trying again: the checkout may already have been removed.

### Installation stops working after a Python upgrade

Reinstall the plugin to rebuild its virtual environment. For a locally linked
installation, rerun `python3 -E -s install.py` in its source directory.

## Trust and safety

Install only code you trust. The installer and plugin run as your user, and Git
configuration, hooks, filters, and merge drivers can execute code. The plugin does
not sandbox them. There are no startup hooks, telemetry, or background agents.

The plugin rechecks the reviewed checkout state before key operations, but cannot
make concurrent writers safe. Stop active writers before confirming. Idle
terminals and finished agents can remain open.

## Contributing and releases

- [Architecture and development](ARCHITECTURE.md): how the workflow works, local
  setup, and tests.
- [Publishing and releases](PUBLISHING.md): release checks and marketplace listing.

## License

[MIT](LICENSE).
