# Publishing and releases

This guide is for maintainers releasing Worktree Tools. Users looking to install
or update the plugin should follow the [README](README.md#install).

## Release checklist

1. Update `version` in `herdr-plugin.toml` and review any changes to minimum Herdr
   or platform requirements.
2. Keep the README’s installation examples and behavior descriptions current.
3. Run the checks below from a fresh installation.
4. Smoke-test the action in stock Herdr on Linux and macOS.
5. Commit the release, push the default branch, and tag the tested commit.
6. Verify installation from the tag and check the marketplace listing.

The plugin ID is `herdr.worktree-tools`. Keep the manifest, CLI adapter, tests,
and documented action commands consistent when changing plugin metadata.

## Automated checks

```sh
python3 -E -s install.py
.venv/bin/python3 -m unittest -v
.venv/bin/python3 -m pip check
git diff --check
```

GitHub Actions runs tests on Linux and macOS with Python 3.11 and 3.13. Tests use
real disposable Git repositories, a fake Herdr CLI, and headless Textual panels.
They do not require a dotfiles repository or modify user worktrees.

When updating dependencies, keep `requirements.txt` and `requirements.lock` in
sync. Resolve dependencies in a fresh temporary virtual environment, review the
complete dependency set, download universal wheels, and record their SHA-256
hashes with `python -m pip hash <wheel>`. Do not generate the lock from a mixed
development environment. Verify fresh installation and tests across the supported
CI platforms and Python versions. The installer accepts only hash-verified wheels.

## Live smoke test

Use an isolated Herdr environment and disposable repository with a linked
worktree. Do not replace an existing plugin registration used for everyday work.
Test with unpatched Herdr at the minimum supported version, currently 0.9.3, on
both Linux and macOS.

Verify:

- The action opens for the selected linked worktree and rejects the main checkout.
- A clean worktree merges without asking for a commit message.
- Uncommitted changes, including untracked and partially staged files, are
  reviewed and committed as documented.
- Canceling before confirmation leaves the repository unchanged.
- Conflicts retain the worktree and provide useful diagnostics.
- Ignored source files are removed with the checkout; ignored target files are
  protected from overwrite.
- Hooks and signing failures show useful diagnostics and recovery information.
- Partial success preserves completed commits or merges and clearly explains
  what to inspect next.
- Successful removal closes the space, retains the source branch, and pushes
  nothing.

CI does not replace these live integration checks. Right-click menu integrations
provided by other projects are outside this plugin’s release requirements.

## Publish a version

After checks pass, commit and push the release changes to `main`. Tag the tested
release commit, using the version from `herdr-plugin.toml`:

```sh
# Example for version 0.3.0; use the new version for subsequent releases.
git tag v0.3.0
git push origin main
git push origin v0.3.0
herdr plugin install ElliotLuque/herdr-worktree-tools --ref v0.3.0
```

Do not move a published release tag to a different commit. Publish a new version
instead. Keep `LICENSE` in the repository and exclude `.venv` and local artifacts.

## Marketplace listing

Herdr’s marketplace discovers public, non-fork, non-archived GitHub repositories
with the `herdr-plugin` topic. The default branch must contain a root
`herdr-plugin.toml` with parseable `id`, `name`, `version`, and
`min_herdr_version` fields. Publishing only a tag or feature branch is not enough.

Set the topic and inspect repository metadata:

```sh
gh repo edit ElliotLuque/herdr-worktree-tools --add-topic herdr-plugin
gh api repos/ElliotLuque/herdr-worktree-tools \
  --jq '{visibility, default_branch, topics, fork, archived}'
```

The index refreshes approximately every 30 minutes and rescans when the default
branch head changes. There is no submission form, upload command, or approval PR.
A marketplace listing is not a security review or endorsement.

References (checked against Herdr 0.9.3):

- [Marketplace documentation](https://herdr.dev/docs/marketplace/)
- [Plugin authoring](https://herdr.dev/docs/plugins/)
- [Marketplace browser](https://herdr.dev/plugins/)

## Downstream packaging

Other installation methods should consume a pinned release and include all five
runtime Python modules: `merge_delete.py`, `worktree_workflow.py`, `commands.py`,
`herdr_client.py`, and `merge_delete_ui.py`. Preserve the executable entrypoint,
launcher contract, and plugin ID. Downstream packaging and optional Herdr patches
are maintained separately from this repository.
