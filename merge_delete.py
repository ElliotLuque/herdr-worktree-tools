"""Herdr action/popup entrypoint; the workflow has no dependency on this module."""

import argparse
import os
import sys

from commands import CommandError, display_text
from herdr_client import HerdrClient, HerdrError
from worktree_workflow import WorktreeWorkflow


def main(argv: list[str] | None = None, *, client: HerdrClient | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", action="store_true", help="Open a popup for the invoking action's workspace")
    args = parser.parse_args(argv)
    herdr = client if client is not None else HerdrClient(os.environ.get("HERDR_BIN_PATH", "herdr"))
    if args.open:
        try:
            herdr.open_popup(os.environ.get("HERDR_WORKSPACE_ID"))
        except (CommandError, HerdrError, OSError) as error:
            print(f"Stopped: {display_text(str(error))}", file=sys.stderr)
            return 1
        return 0
    # Popup focus context is not selection authority. Never fall back to cwd/focus.
    workspace_id = os.environ.get("HERDR_MERGE_WORKSPACE_ID")
    from merge_delete_ui import MergeDeleteApp
    return MergeDeleteApp(WorktreeWorkflow(herdr), workspace_id).run() or 0


if __name__ == "__main__":
    sys.exit(main())
