#!/usr/bin/env python3
"""One attempt directory, read back against docs/design/ue-identity-continuity.md.

usage: identity_check.py <formal38guarded-...>  (defaults to the newest attempt)
Prints what the identity-continuity path did in that attempt; changes nothing.
"""
import glob, json, sys
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent


def main(argv):
    target = Path(argv[1]) if len(argv) > 1 else max(EXP.glob('formal38guarded-*'), key=lambda p: p.stat().st_mtime)
    target = target if target.is_absolute() else EXP / target
    print('attempt', target.name)
    exit_path = target / 'exit.json'
    report = json.loads(exit_path.read_text()) if exit_path.is_file() else {}
    print('submissionStatus', report.get('submissionStatus'), 'failure', (report.get('failure') or {}).get('code'))
    profile_path = target / 'profile.json'
    if profile_path.is_file():
        block = json.loads(profile_path.read_text()).get('liveConsole', {})
        print('ueHosts', block.get('ueHosts'), 'ueIdentityPath', bool(block.get('ueIdentityPath')))
    identity = target / 'ue-identity.json'
    if identity.is_file():
        print('ue-identity.json', {role: row.get('amfUeNgapId') for role, row in
                                   json.loads(identity.read_text()).get('roles', {}).items()})
    refreshes = (report.get('controlHeaders') or {}).get('refreshes') or []
    print('header refreshes', len(refreshes), 'rejoins',
          sum(1 for row in refreshes if 'rejoined' in row), 'errors',
          sum(1 for row in refreshes if 'errorType' in row or 'rejoinErrorType' in row))
    print('tunRebinds (sender retargets)', report.get('tunRebinds'))
    for path in sorted(glob.glob(str(target / 'sources' / 'ue*' / '*.jsonl'))):
        events = []
        for line in Path(path).read_text(errors='ignore').splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        kinds = [row.get('event') for row in events]
        epochs = [row.get('bindEpoch') for row in events if row.get('event') == 'rebind']
        print(f"  {Path(path).parent.name}/{Path(path).name}: rebind-wait={kinds.count('rebind-wait')} "
              f"rebind={kinds.count('rebind')} epochs={epochs} end={events[-1].get('status') if events else None}")
    for path in glob.glob(str(target / 'evidence' / '*-episode.json')):
        episode = json.loads(Path(path).read_text())
        print('experimentVersion', episode.get('experimentVersion'))
        print('termination', episode.get('termination'))
        print('intents ueIds', sorted({row.get('ueId') for row in episode.get('intents', [])
                                       if isinstance(row, dict)}))
        for item in episode.get('identityRebinds') or []:
            print('  rebind', item.get('index'), item.get('outcome'), {ue: (v.get('previousAmfUeNgapId'), v.get('amfUeNgapId'))
                  for ue, v in (item.get('ues') or {}).items()}, item.get('detail', ''))
        for item in episode.get('hardwareDisconnects') or []:
            print('  disconnect', sorted(item.get('ues') or ()), 'reregistered', item.get('reregistered'),
                  'waitedMs', item.get('waitedMs'), 'trials', [t.get('terminalState') for t in item.get('trials') or ()])
        print('unresolved', (episode.get('completion') or {}).get('unresolved'))
        execution = episode.get('execution') or {}
        print('retiredCases', [case.get('caseId') for case in execution.get('retiredCases') or []])
        events = Path(path).with_name(Path(path).name.replace('-episode.json', '-events.jsonl'))
        if events.is_file():
            joint = {}
            for line in events.read_text().splitlines():
                row = json.loads(line)
                if row.get('eventKind') == 'RawSampleIngested' or row.get('event_kind') == 'RawSampleIngested':
                    sample = (row.get('payload') or {}).get('sample') or row.get('payload') or {}
                    counter = str(sample.get('counterId'))
                    if '/joint@' in counter:
                        seen = joint.setdefault(counter, [0, 0])
                        seen[0] += 1
                        seen[1] += 0 if sample.get('missingIntervals') else 1
            print('per-UE Kernel counters (samples, with data):', joint)


if __name__ == '__main__':
    main(sys.argv)
