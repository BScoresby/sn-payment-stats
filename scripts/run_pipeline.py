#!/usr/bin/env python3
"""Run collection and summarization with one command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent


def main() -> int:
    arguments = sys.argv[1:]
    collect = [sys.executable, str(SCRIPTS_DIR / "collect.py"), *arguments]
    collect_rewards = [
        sys.executable, str(SCRIPTS_DIR / "collect_rewards.py"), *arguments
    ]
    subprocess.run(collect, check=True)
    subprocess.run(collect_rewards, check=True)
    if "--dry-run" not in sys.argv[1:]:
        subprocess.run([sys.executable, str(SCRIPTS_DIR / "summarize.py")], check=True)
        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "summarize_rewards.py")], check=True
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
