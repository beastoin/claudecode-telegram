#!/usr/bin/env python3
"""Minimal tmux adapter for codex-backed workers."""

import subprocess
import sys
import time


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: codex-tmux-adapter.py <tmux-session> <message>", file=sys.stderr)
        return 1

    tmux_name = sys.argv[1]
    message = sys.argv[2]

    if subprocess.run(["tmux", "send-keys", "-t", tmux_name, "-l", message]).returncode != 0:
        return 1

    time.sleep(0.2)

    if subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"]).returncode != 0:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
