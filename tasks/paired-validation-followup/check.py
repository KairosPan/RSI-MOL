"""Check the follow-up deliverables. Matching citations to actual session receipts is a separate owner audit."""
import json
import re
import runpy
import sys
from pathlib import Path


def check(folder):
    paired = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'paired-validation' / 'check.py'))
    result = paired['check'](folder)
    if result != 'passed':
        return result
    for row in json.loads(paired['read'](folder, 'evidence.json')):
        source = row.get('delivery_source')
        if source not in ('memory', 'literature', 'both'):
            return 'Identify the delivery source for each citation.'
        if source in ('memory', 'both') and not (
                isinstance(row.get('evidence_id'), str) and row['evidence_id'].strip()
                and type(row.get('revision')) is int and row['revision'] > 0
                and isinstance(row.get('source_run'), str) and re.fullmatch(r'R[0-9]{4,}', row['source_run'])):
            return 'A memory citation needs its evidence ID, positive revision and historical source run.'
    return 'passed'


if __name__ == '__main__':
    try:
        message = check(Path(sys.argv[1]))
    except (OSError, ValueError, RecursionError):
        message = 'The memo and evidence array must be readable.'
    print(message)
    sys.exit(0 if message == 'passed' else 1)
