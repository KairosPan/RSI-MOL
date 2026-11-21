"""Check the two deliverables and evidence addresses. The owner reviews scientific support separately."""
import json
import os
import re
import stat
import sys
from pathlib import Path


def read(folder, name):
    fd = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024:
            raise ValueError('Expected a bounded regular file')
        return stream.read(256 * 1024 + 1).decode('utf-8')


def check(folder):
    report = read(folder, 'report.md')
    rows = json.loads(read(folder, 'evidence.json'))
    if not report.strip() or not isinstance(rows, list):
        return 'A decision memo and an evidence array are required'
    dois, originals = set(), set()
    for row in rows:
        fields = ('doi', 'claim', 'quote', 'document_id', 'document_version', 'source_sha256', 'locator')
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip() for k in fields):
            return 'An evidence entry is missing a required field'
        if (not re.fullmatch(r'10\.\d{4,9}/\S+', row['doi'], re.I)
                or not re.fullmatch(r'doc_[0-9a-f]{64}', row['document_id'])
                or not re.fullmatch(r'[0-9a-f]{64}', row['document_version'])
                or not re.fullmatch(r'[0-9a-f]{64}', row['source_sha256'])
                or not re.fullmatch(r'block:\d{6}', row['locator'])):
            return 'A paper or passage address is invalid'
        dois.add(row['doi'].lower())
        originals.add(row['source_sha256'])
    if len(dois) < 2 or len(originals) < 2:
        return 'Evidence from two distinct papers is required'
    return 'passed'


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('usage: check.py <output folder>', file=sys.stderr)
        sys.exit(2)
    try:
        message = check(Path(sys.argv[1]))
    except (OSError, ValueError, RecursionError):
        message = 'Both deliverables must be readable and evidence.json must contain valid JSON'
    print(message)
    sys.exit(0 if message == 'passed' else 1)
