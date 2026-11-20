"""Check of the grid task: <results folder>/answer.json is a JSON object with exactly two integer fields, x and y,
both from -10 to 10, at which f takes its largest value on that grid.

The control program runs it with its own Python, standard library only, the collected results folder as argv[1].
It prints exactly one line, which goes into the record Meta reads, so it never shows the expected values; the exit
code is 0 only when the answer passes.
"""
import json
import sys
from pathlib import Path

LIMIT = 1 << 20                         # bytes of answer.json read at most; a longer file does not pass
GRID = range(-10, 11)


def f(x: int, y: int) -> int:
    return -((x - 3) ** 2 + (y + 1) ** 2)


class Pairs(list):
    """A JSON object as the list of its (key, value) pairs, so that a repeated key is seen."""


def verdict(folder: Path) -> str:
    path = folder / 'answer.json'
    if not path.is_file():
        return '缺少 answer.json'
    try:
        with open(path, 'rb') as stream:
            data = stream.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('too long')
        answer = json.loads(data.decode('utf-8'), object_pairs_hook=Pairs)
    except (OSError, ValueError, RecursionError):
        return 'answer.json 不是合法的 JSON 对象'
    if not isinstance(answer, Pairs) or sorted(key for key, _ in answer) != ['x', 'y']:
        return '字段不符合要求'
    values = dict(answer)
    if not all(type(values[key]) is int for key in ('x', 'y')):
        return '字段不符合要求'
    best = max(f(x, y) for x in GRID for y in GRID)
    if values['x'] not in GRID or values['y'] not in GRID or f(values['x'], values['y']) != best:
        return 'x、y 没有使 f 取到最大值'
    return '通过'


if __name__ == '__main__':
    message = verdict(Path(sys.argv[1]))
    print(message)
    sys.exit(0 if message == '通过' else 1)
