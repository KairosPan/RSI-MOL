"""Start a command in a POSIX process group or a kill-on-close Windows job.

On Windows a trusted Python bootstrap joins the new job before it starts the
requested command. If the controller disappears, its job handle closes and
Windows ends the whole tree. No recovery operation signals a recycled PID.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes as wt
    import secrets

    class _BasicLimits(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                    ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', wt.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wt.DWORD), ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wt.DWORD), ('SchedulingClass', wt.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _BasicLimits), ('IoInfo', ctypes.c_ulonglong * 6),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    class _JobAccounting(ctypes.Structure):
        _fields_ = [('times', ctypes.c_longlong * 4), ('total_faults', wt.DWORD),
                    ('total_processes', wt.DWORD), ('active_processes', wt.DWORD),
                    ('terminated_processes', wt.DWORD)]

    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)

    def _checked(result, function, arguments):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def _api(name, result, *arguments):
        function = getattr(_kernel, name)
        function.restype, function.argtypes = result, arguments
        function.errcheck = _checked
        return function

    _create = _api('CreateJobObjectW', wt.HANDLE, wt.LPVOID, wt.LPCWSTR)
    _set = _api('SetInformationJobObject', wt.BOOL, wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD)
    _open = _api('OpenJobObjectW', wt.HANDLE, wt.DWORD, wt.BOOL, wt.LPCWSTR)
    _assign = _api('AssignProcessToJobObject', wt.BOOL, wt.HANDLE, wt.HANDLE)
    _terminate = _api('TerminateJobObject', wt.BOOL, wt.HANDLE, wt.UINT)
    _query = _api('QueryInformationJobObject', wt.BOOL, wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD, wt.LPVOID)
    _close = _api('CloseHandle', wt.BOOL, wt.HANDLE)
    _peek = _api('PeekNamedPipe', wt.BOOL, wt.HANDLE, wt.LPVOID, wt.DWORD, wt.LPVOID,
                 ctypes.POINTER(wt.DWORD), wt.LPVOID)
    _kernel.GetCurrentProcess.restype = wt.HANDLE

    def read_available(fd, limit):
        """Read at most limit bytes of a Windows pipe without waiting for another writer."""
        import msvcrt
        available = wt.DWORD()
        try:
            _peek(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None)
        except OSError as error:
            if error.winerror == 109:  # ERROR_BROKEN_PIPE
                return b''
            raise
        return os.read(fd, min(limit, available.value)) if available.value else b''

    def _bootstrap(name, command):
        handle = _open(0x0001, False, name)  # JOB_OBJECT_ASSIGN_PROCESS
        try:
            _assign(handle, _kernel.GetCurrentProcess())
        finally:
            _close(handle)
        # Only the controller retains a job handle. All children inherit job membership.
        return subprocess.call(command, stdin=0, stdout=1, stderr=2, close_fds=True)


def start_group(command, **options):
    """Popen with a separately terminable tree. Always call kill_group in a finally."""
    if os.name != 'nt':
        return subprocess.Popen(command, start_new_session=True, **options)
    name = 'Local\\RSI-' + secrets.token_hex(24)
    handle = _create(None, name)
    try:
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _set(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        # An isolated base Python avoids a venv redirector and ignores the child's cwd/PYTHONPATH.
        python = getattr(sys, '_base_executable', sys.executable)
        args = [python, '-I', '-S', '-B', str(Path(__file__).resolve()), name, *command]
        if len(subprocess.list2cmdline(args).encode('utf-16-le')) // 2 >= 32767:
            raise OSError('The composed command exceeds the 32767-character Windows process limit')
        process = subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, **options)
        process._rsi_job_handle = handle
        return process
    except BaseException:
        _close(handle)
        raise


def kill_group(process):
    """Kill remaining members, even if the group's leader has already exited."""
    if os.name == 'nt':
        import time
        handle = process._rsi_job_handle
        if handle is None:
            return
        try:
            # A timeout can fire before the bootstrap joins the job. Stop it first
            # so that no command can be started after we observe an empty job.
            if process.poll() is None:
                process.kill()
                process.wait()
            _terminate(handle, 1)
            # Do not hand the tree back to the filesystem collector while children still run.
            deadline = time.monotonic() + 15
            accounting = _JobAccounting()
            while True:
                _query(handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None)
                if not accounting.active_processes:
                    break
                if time.monotonic() >= deadline:
                    raise OSError('Timed out waiting for Windows job termination')
                time.sleep(0.01)
        finally:
            process._rsi_job_handle = None
            _close(handle)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def stop_group(process, grace):
    """Stop a running group, escalating after the grace period on POSIX, and reap its leader."""
    try:
        if os.name != 'nt':
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
    finally:
        kill_group(process)
        process.wait()


if __name__ == '__main__' and os.name == 'nt':
    sys.exit(_bootstrap(sys.argv[1], sys.argv[2:]))
