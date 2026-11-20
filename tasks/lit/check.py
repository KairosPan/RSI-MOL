"""Check of the lit task: <results folder>/answer.md exists, is not empty and names at least one arXiv id or DOI.
It does not judge the answer. One line is printed; the exit code is 0 only when the file passes."""
import re
import sys
from pathlib import Path

LIMIT = 1 << 20
SOURCE = re.compile(r'(arXiv\s*:?\s*\d{4}\.\d{4,5})|(\b10\.\d{4,9}/\S+)', re.IGNORECASE)


def verdict(folder: Path) -> str:
    path = folder / 'answer.md'
    if not path.is_file():
        return 'answer.md is missing'
    try:
        text = path.read_bytes()[:LIMIT].decode('utf-8', 'replace')
    except OSError:
        return 'answer.md cannot be read'
    if not text.strip():
        return 'answer.md is empty'
    if not SOURCE.search(text):
        return 'it names no arXiv id or DOI'
    return 'passed'


if __name__ == '__main__':
    message = verdict(Path(sys.argv[1]))
    print(message)
    sys.exit(0 if message == 'passed' else 1)
