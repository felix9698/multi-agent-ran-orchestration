#!/usr/bin/env python3
"""Re-key a T and a C formed earlier onto the UE identities a run addresses.

exp_metrics.md section 1: "Reuse T/C when their defining inputs remain unchanged."
The defining inputs here -- the intents, the owner authorization, the function
catalog and the action space -- are hash-pinned and do not move between attempts;
the only thing that changes is which amfUeNgapId each host currently holds. So the
board is reused and only its identity keys are substituted, which takes the Target
and Control calls off the live path entirely.

    python3 prepare_tc.py <episode.json> ue1=459,ue2=458,ue3=457 <out.json>

The episode's own record carries T and C with their schemaVersion, so nothing is
invented: the output is that record with one identity swapped for another.
"""
import json
import re
import sys
from pathlib import Path


def rekey(document, mapping):
    """Swap every old UE id for its new one, everywhere, in one pass.

    A single regex alternation avoids the trap of chained replacements, where
    swapping 424->425 and then 425->426 would carry the first result into the
    second.
    """
    if not mapping:
        return document
    pattern = re.compile(r'(?<![0-9])(' + '|'.join(re.escape(k) for k in mapping) + r')(?![0-9])')
    swap = lambda m: mapping[m.group(1)]

    def walk(value):
        """Substitute inside strings only.

        A whole-document regex over the serialised JSON would also rewrite an
        amfUeNgapId that happens to be stored as a number, which is a different
        field with a different meaning from the ``servingCell@<id>`` axis keys
        this is here to re-key.
        """
        if isinstance(value, str):
            return pattern.sub(swap, value)
        if isinstance(value, dict):
            return {pattern.sub(swap, k) if isinstance(k, str) else k: walk(v)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(document)


def main(argv):
    if len(argv) != 3:
        raise SystemExit(__doc__)
    episode = json.loads(Path(argv[0]).read_text(encoding='utf-8'))
    old_hosts = {host: ue for ue, host in (episode.get('ueHosts') or {}).items()}
    mapping = {}
    for item in argv[1].split(','):
        host, _, new = item.partition('=')
        host, new = host.strip(), new.strip()
        if not new:
            continue
        old = old_hosts.get(host)
        if old is None:
            raise SystemExit(f'the episode does not say which id {host} held; '
                             f'it knows {sorted(old_hosts)}')
        if old != new:
            mapping[old] = new
    out = {'T': rekey(episode['T'], mapping), 'C': rekey(episode['C'], mapping),
           'preparedFrom': {'episodeId': episode.get('episodeId'),
                            'prepMs': (episode.get('timing') or {}).get('prepMs'),
                            'rekeyed': mapping}}
    Path(argv[2]).write_text(json.dumps(out, indent=1), encoding='utf-8')
    print(json.dumps({'wrote': argv[2], 'rekeyed': mapping,
                      'candidates': len(out['C'].get('candidates') or []),
                      'alternatives': len(out['T'].get('alternatives') or [])}))
    return 0


def _selfcheck():
    doc = {'a': 'servingCell@424', 'b': {'dlPrbCap@425': '0'}, 'c': ['424', 4240, '1424']}
    out = rekey(doc, {'424': '425', '425': '426'})
    assert out['a'] == 'servingCell@425', out
    assert out['b'] == {'dlPrbCap@426': '0'}, out          # not chained into 427
    assert out['c'] == ['425', 4240, '1424'], out          # digit-bounded, and numbers are left alone
    print('selfcheck OK')


if __name__ == '__main__':
    if sys.argv[1:2] == ['--selfcheck']:
        _selfcheck()
    else:
        raise SystemExit(main(sys.argv[1:]))
