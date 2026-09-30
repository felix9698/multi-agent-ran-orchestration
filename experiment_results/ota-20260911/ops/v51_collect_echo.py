#!/usr/bin/env python3
"""Keep the raw UE2 tagged-echo records of the v5.1 pilot (owner/GPT 2026-09-26: a p60 deadline
must be re-scorable from the same observations).  Each board's per-request log and each load's
reference echoes live only in ue2:/tmp, which a reboot clears; this copies them next to the
board evidence (<board>/evidence/ue2-echo-client.jsonl); the references already carry their raw
RTTs.  Idempotent.
"""
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def fetch(remote: str, local: Path) -> str:
    if local.exists() and local.stat().st_size > 0:
        return 'kept'
    local.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['scp', '-q', f'ue2:{remote}', str(local)], capture_output=True, timeout=120)
    return 'copied' if r.returncode == 0 else f'missing ({r.stderr.decode()[:60].strip()})'


def main():
    for log in sorted((HERE / 'overnight').glob('v51-pilot*.log')):
        for line in log.read_text(errors='ignore').splitlines():
            m = re.search(r' part \d+ attempt \d+ dir=(formal38guarded-\S+)', line)
            if m:
                d = m.group(1)
                print(d, fetch(f'/tmp/aic-{d}/ue2-echo-client.jsonl', ROOT / d / 'evidence' / 'ue2-echo-client.jsonl'))
    # The references already keep their raw echo RTTs (echoRttsMs / echoRequests).


if __name__ == '__main__':
    main()
