#!/bin/sh
# Herdr runs commands in the plugin root; also support invocation from elsewhere.
set -eu
root=${HERDR_PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}

# Nix supplies a wrapped interpreter; marketplace installs use an isolated venv.
if [ -x "$root/bin/merge-delete" ]; then
    exec "$root/bin/merge-delete" "$@"
fi
if [ -x "$root/.venv/bin/python3" ]; then
    # Ignore inherited Python import overrides and user site-packages.
    exec "$root/.venv/bin/python3" -E -s "$root/merge_delete.py" "$@"
fi
printf '%s\n' 'Worktree Tools is not built. Run python3 -E -s install.py in the plugin directory, then link it again.' >&2
exit 1
