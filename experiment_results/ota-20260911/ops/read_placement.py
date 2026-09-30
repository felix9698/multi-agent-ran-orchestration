"""Print the UE->cell placement as `ue1=gnb1 ue2=gnb1 ue3=gnb2`, by reading the text.

Never import conductor for this.  Importing it runs the epoch-pin helper, which does a
`docker exec ... cat flexric-connection-witness.json` and prints a GATE_EPOCH_PIN line;
harmless in itself, but inside a shell command substitution that line becomes the cell
name and force_reattach is handed a JSON blob.  Caught 2026-09-16 18:09, before the
maintenance daemon had ever fired.
"""
import re
import sys
from pathlib import Path

src = (Path(__file__).with_name('conductor.py')).read_text(encoding='utf-8')
found = re.search(r"INITIAL_CELLS\s*=\s*\{([^}]*)\}", src)
pairs = dict(re.findall(r"'(ue[123])'\s*:\s*'(gnb[12])'", found.group(1))) if found else {}
if set(pairs) != {'ue1', 'ue2', 'ue3'}:
    sys.exit(f'conductor.INITIAL_CELLS unreadable: {pairs}')
print(' '.join(f'{ue}={pairs[ue]}' for ue in ('ue1', 'ue2', 'ue3')))

if __name__ == '__main__' and '--self-check' in sys.argv:
    assert re.findall(r"'(ue[123])'\s*:\s*'(gnb[12])'", "{'ue1': 'gnb1', 'ue3': 'gnb2'}") == \
        [('ue1', 'gnb1'), ('ue3', 'gnb2')]
