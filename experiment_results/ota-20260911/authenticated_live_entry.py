#!/usr/bin/env python3
"""Compatibility entry for main.py using the caller's provider configuration.

Importing this module has no side effect: the top level only defines the guard
constants and main(). Credentials and model routes are never rewritten.
Temporary argv and import-path changes are restored before main() returns.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUTHENTICATION_HOLD = Path(__file__).with_name('live-authentication-hold.json')


def main(argv=None):
    # Never print or persist either credential or the configured origin.
    if AUTHENTICATION_HOLD.exists():
        raise SystemExit('LIVE_MODEL_AUTHENTICATION_ON_HOLD: ' + str(AUTHENTICATION_HOLD))
    argv = sys.argv[1:] if argv is None else list(argv)

    repo = str(REPO)
    added_repo = repo not in sys.path
    saved_argv = sys.argv
    if added_repo:
        sys.path.insert(0, repo)
    import main as main_module
    sys.argv = [str(REPO / 'main.py')] + argv
    try:
        return main_module.main() or 0
    finally:
        sys.argv = saved_argv
        if added_repo:
            try:
                sys.path.remove(repo)
            except ValueError:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
