"""Bound the pinned Poppler adapter's output and runtime on Windows."""
import stat
import subprocess
import time

from rsi_literature.parsers import _ParserError, _number
from _processes import kill_group, read_available, start_group


def _check_artifacts(limits):
    for path, maximum in limits.items():
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or info.st_size > maximum):
            raise _ParserError('output_limit', 'Parser artifact exceeds its host byte budget')


def execute(argv, job, settings, provenance, env=None, artifact_limits=None):
    """Run one host-selected Poppler tool in a job, draining its pipe without blocking."""
    timeout = _number(settings, 'timeout', 120, 1, 1800)
    limit = int(_number(settings, 'max_log_bytes', 65536, 1024, 1024 * 1024))
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        process = start_group(argv, cwd=job, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=env)
    except (FileNotFoundError, PermissionError) as error:
        raise _ParserError('needs_configuration', 'The configured parser could not start',
                           'unavailable', 'after_configuration') from error
    provenance['attempt_count'] += 1
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ParserError('parser_timeout', 'Parser exceeded its host time budget')
            _check_artifacts(artifact_limits or {})
            code = process.poll()
            data = read_available(process.stdout.fileno(), min(65536, limit - len(output) + 1))
            if data:
                output.extend(data)
                if len(output) > limit:
                    raise _ParserError('output_limit', 'Parser logs exceeded their byte budget')
            elif code is not None:
                break
            else:
                time.sleep(min(remaining, 0.025))
        if process.returncode:
            raise _ParserError('parser_exit', f'Native parser exited with status {process.returncode}',
                               retryability='after_configuration')
        return bytes(output)
    finally:
        try:
            kill_group(process)
        finally:
            process.stdout.close()
            process.wait()
