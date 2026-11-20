"""Check of the net task: <results folder>/result.json is a JSON array of three objects, each with the fields
command, exit_code (an integer), stdout and stderr. It checks the shape only; what the three attempts returned is
read from the record by the owner. One line is printed; the exit code is 0 only when the shape is right."""
import json
import sys
from pathlib import Path

LIMIT = 1 << 20


def verdict(folder: Path) -> str:
    path = folder / 'result.json'
    if not path.is_file():
        return 'result.json is missing'
    try:
        data = path.read_bytes()[:LIMIT + 1]
        if len(data) > LIMIT:
            raise ValueError('too long')
        result = json.loads(data.decode('utf-8'))
    except (OSError, ValueError, RecursionError):
        return 'result.json is not valid JSON'
    if not isinstance(result, list) or len(result) != 3:
        return 'it is not an array of three objects'
    for item in result:
        if not isinstance(item, dict) or not {'command', 'exit_code', 'stdout', 'stderr'} <= set(item):
            return 'an object lacks a field'
        if type(item['exit_code']) is not int:
            return 'exit_code is not an integer'
    return 'passed'


if __name__ == '__main__':
    message = verdict(Path(sys.argv[1]))
    print(message)
    sys.exit(0 if message == 'passed' else 1)
