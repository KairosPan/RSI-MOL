"""Native Windows process-lifetime guarantees for the managed Codex and check runners."""
import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rsi.processes import kill_group, start_group


@unittest.skipUnless(os.name == 'nt', 'Windows job objects')
class WindowsJobs(unittest.TestCase):
    def test_child_is_stopped_after_its_parent_exits(self):
        # Hold a kernel handle to check the child's identity even if a PID could be reused.
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        child = 'import time; time.sleep(15)'
        parent = ('import subprocess, sys; '
                  'p=subprocess.Popen([sys.executable,"-c",sys.argv[1]],stdin=subprocess.DEVNULL,'
                  'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid)')
        proc = start_group([sys.executable, '-c', parent, child], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = proc.communicate(timeout=8)
            self.assertEqual(proc.returncode, 0, err)
            handle = kernel.OpenProcess(0x100000, False, int(out))
            self.assertTrue(handle)
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)  # WAIT_TIMEOUT
                kill_group(proc)
                self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0)
            finally:
                kernel.CloseHandle(handle)
        finally:
            kill_group(proc)
            proc.wait(timeout=8)

    def test_checker_drains_and_caps_output_and_times_out(self):
        from rsi.tasks import CHECK_TIMED_OUT, Task, run_check
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            check = root / 'check.py'
            task = Task('T1', {}, root, check, None)
            check.write_text('print("界"*2000)\nprint("x"*1048576)\nraise SystemExit(3)', encoding='utf-8')
            result = run_check(task, root)
            self.assertEqual(result, {'passed': False, 'exit_code': 3, 'message': '界' * 500})
            check.write_text('import time; time.sleep(5)', encoding='utf-8')
            self.assertEqual(run_check(task, root, timeout=0.15),
                             {'passed': False, 'exit_code': None, 'message': CHECK_TIMED_OUT})
