"""Shim: all content merged into bridge.py."""
import sys
import types

import bridge
from bridge import *  # noqa: F401,F403
from bridge import (  # noqa: F401 — underscore names excluded by * import
    _bare_repo_url,
    _build_teleport_context,
    _ensure_bare_repo,
    _ensure_workspace_trusted_remote,
    _get_claude_pid,
    _get_project_name,
    _git_pull_state,
    _git_push_state,
    _remote_run,
    _subprocess_runner,
)


class _TeleportShimModule(types.ModuleType):
    """Propagate attribute writes to bridge (for test monkeypatching)."""
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if hasattr(bridge, name):
            setattr(bridge, name, value)


sys.modules[__name__].__class__ = _TeleportShimModule
