"""CLI: run the upper-artifact self-test against a separately spawned upper.

    python3 -m oran.release.ubm_selftest --work-dir <dir> --report <file.json>

``--upper-command`` points the driver at the release runtime once it exists; the
literal ``{startup}`` is replaced with the generated startup file.  Without it
the driver falls back to the in-package stand-in and says so in the report.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .driver import BILATERAL_SCENARIOS, SelfTestConfig, run_selftest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oran.release.ubm_selftest")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument("--upper-command", nargs="*", default=None,
                        help="argv of the upper process; {startup} is substituted")
    parser.add_argument("--upper-label", default=None)
    parser.add_argument("--release-root", default=None,
                        help="root of an extracted release archive; the upper under "
                             "test becomes that release's own bin/ubm and every "
                             "spec/contract byte is read out of the release")
    parser.add_argument("--scenario", action="append", default=None)
    arguments = parser.parse_args(argv)

    work_dir = Path(arguments.work_dir) if arguments.work_dir else Path(
        tempfile.mkdtemp(prefix="ubm-selftest-"))
    release_root = Path(arguments.release_root).resolve() if arguments.release_root else None
    if release_root is None:
        # Auto-detect: when this package was unpacked from a release the layout is
        # <release>/selftest/, and the release beside it is the artifact to drive.
        candidate = Path(__file__).resolve().parents[1]
        if (candidate / "RELEASE-MANIFEST.json").is_file() and (
                candidate / "bin" / "ubm").is_file():
            release_root = candidate
    config = SelfTestConfig(
        repo_root=Path(arguments.repo_root).resolve(),
        work_dir=work_dir,
        upper_command=tuple(arguments.upper_command) if arguments.upper_command else None,
        upper_label=arguments.upper_label,
        release_root=release_root,
        scenarios=tuple(arguments.scenario) if arguments.scenario else BILATERAL_SCENARIOS,
    )
    report = run_selftest(config)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if arguments.report:
        Path(arguments.report).write_text(rendered + "\n", encoding="utf-8")
    else:
        sys.stdout.write(rendered + "\n")
    return 0 if report["suiteSatisfied"] else 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
