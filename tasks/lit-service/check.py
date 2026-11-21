"""Check the submitted answer. The Stage L audit separately checks the original and the tool receipts."""
import json
import os
import re
import stat
import sys
from pathlib import Path


def verdict(data):
    fields = ('answer', 'doi', 'pmcid', 'document_id', 'document_version', 'source_sha256',
              'locator', 'quote', 'reference_title', 'coverage')
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k].strip() for k in fields):
        return 'The answer is missing a required text field'
    if data['doi'].lower() != '10.1093/bioinformatics/btp352' or data['pmcid'].upper() != 'PMC2723002':
        return 'The paper identifiers do not match the requested paper'
    expected = {'document_id': r'doc_[0-9a-f]{64}', 'source_sha256': r'[0-9a-f]{64}',
                'document_version': r'[0-9a-f]{64}', 'locator': r'block:[0-9]{6}'}
    if not all(re.fullmatch(pattern, data[key]) for key, pattern in expected.items()):
        return 'The document identifier, version, hash or body locator has the wrong form'
    answer = data['answer'].lower()
    if not all(term in answer for term in ('header', 'alignment', '@')):
        return 'The answer does not describe both sections and their distinguishing character'
    return 'passed'


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('usage: check.py <output folder>', file=sys.stderr)
        sys.exit(2)
    try:
        fd = os.open(Path(sys.argv[1]) / 'answer.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('Expected a regular file')
            raw = stream.read(65537)
        result = 'The answer exceeds 64 KiB' if len(raw) > 65536 else verdict(json.loads(raw))
    except (OSError, ValueError, RecursionError):
        result = 'A readable answer.json is required'
    print(result)
    sys.exit(0 if result == 'passed' else 1)
