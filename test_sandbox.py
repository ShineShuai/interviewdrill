import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sandbox


class SandboxTest(unittest.TestCase):
    def test_stdin_stdout(self):
        r = sandbox.run(["cat"], "hello\n")
        self.assertEqual((r.ok, r.out), (True, "hello\n"))

    def test_timeout_kills_process_group(self):
        r = sandbox.run(["sh", "-c", "sleep 30 & sleep 30"], timeout=0.3)
        self.assertTrue(r.timeout)
        self.assertFalse(r.ok)
        self.assertLess(r.secs, 3)

    def test_missing_command(self):
        self.assertEqual(sandbox.run(["/no/such/binary"]).rc, 127)

    def test_output_is_capped(self):
        code = "import sys\nwhile True: sys.stdout.write('x' * 65536)"
        r = sandbox.run([sys.executable, "-c", code], timeout=20)
        self.assertFalse(r.ok)
        self.assertLessEqual(len(r.out), sandbox.MAX_OUT)

    def test_file_size_cap_applies_to_runs_but_not_to_builds(self):
        write = "import sys\nopen(sys.argv[1], 'wb').write(bytes(6 << 20))"
        with tempfile.TemporaryDirectory() as d:
            big = str(Path(d, "big"))
            self.assertFalse(sandbox.run([sys.executable, "-c", write, big]).ok)
            r = sandbox.run([sys.executable, "-c", write, big], fsize=sandbox.BUILD_FSIZE)
            self.assertTrue(r.ok, r.err)
            self.assertEqual(Path(big).stat().st_size, 6 << 20)

    @unittest.skipUnless(sys.platform == "linux", "RLIMIT_AS is Linux-only")
    def test_memory_limit(self):
        r = sandbox.run([sys.executable, "-c", "x = bytearray(1 << 30)"], mem_mb=200)
        self.assertFalse(r.ok)

    def test_cpu_limit_stops_busy_loop(self):
        r = sandbox.run([sys.executable, "-c", "while True: pass"], timeout=1)
        self.assertFalse(r.ok)

    def test_run_py_args_and_isolation(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d, "s.py")
            script.write_text("import sys\nprint(sys.argv[1:], sys.stdin.read().strip())")
            r = sandbox.run_py(script, "1", "2", stdin="in")
            self.assertEqual(r.out.strip(), "['1', '2'] in")



class DockerCommandTest(unittest.TestCase):
    """DRILL_SANDBOX=docker without a Docker daemon: the command and the cleanup."""

    def run_py(self, timed_out):
        fake = sandbox.Result(-1 if timed_out else 0, "", "", 1.0, timed_out)
        with mock.patch.dict(os.environ, {"DRILL_SANDBOX": "docker"}), \
                mock.patch.object(sandbox, "run", return_value=fake) as run, \
                mock.patch.object(subprocess, "run") as sub:
            sandbox.run_py(Path("/tmp/x/s.py"), "arg", stdin="in", timeout=2)
        return run.call_args.args[0], run.call_args.kwargs, sub

    def test_command_is_locked_down_and_named(self):
        cmd, _, _ = self.run_py(False)
        for flag in ("--network", "--read-only", "--cap-drop", "--pids-limit", "--rm"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--network") + 1], "none")
        self.assertEqual(cmd[cmd.index("--cap-drop") + 1], "ALL")
        self.assertEqual(cmd[-4:], ["python", "-I", "s.py", "arg"])
        self.assertRegex(cmd[cmd.index("--name") + 1], r"^drill-[0-9a-f]{12}$")

    def test_a_timeout_kills_the_container_by_name(self):
        cmd, _, sub = self.run_py(True)
        sub.assert_called_once()
        self.assertEqual(sub.call_args.args[0], ["docker", "kill", cmd[cmd.index("--name") + 1]])

    def test_no_kill_when_the_script_finished(self):
        _, _, sub = self.run_py(False)
        sub.assert_not_called()


def docker_ready() -> bool:
    try:
        return all(subprocess.run(c, capture_output=True, timeout=20).returncode == 0
                   for c in (["docker", "info"], ["docker", "image", "inspect", "python:3.12-slim"]))
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(docker_ready(), "needs a running Docker with the image python:3.12-slim")
class RealDockerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"DRILL_SANDBOX": "docker"})
        env.start()
        self.addCleanup(env.stop)

    def py(self, code, *args, stdin="", timeout=5.0):
        script = self.dir / "s.py"
        script.write_text(code)
        return sandbox.run_py(script, *args, stdin=stdin, timeout=timeout)

    def test_stdin_args_and_output(self):
        r = self.py("import sys\nprint(sys.stdin.read().upper(), sys.argv[1:])", "a", stdin="hi")
        self.assertEqual((r.ok, r.out), (True, "HI ['a']\n"))

    def test_no_network_and_a_read_only_mount(self):
        r = self.py("import socket\n"
                    "try:\n    socket.create_connection(('1.1.1.1', 80), timeout=2); print('NET')\n"
                    "except OSError: print('blocked')\n"
                    "try:\n    open('/w/x', 'w'); print('WROTE')\nexcept OSError: print('ro')")
        self.assertEqual(r.out.split(), ["blocked", "ro"])

    def test_memory_limit_kills_the_script(self):
        self.assertFalse(self.py("x = bytearray(1 << 30)", timeout=10).ok)

    def test_a_timeout_leaves_no_container_running(self):
        running = lambda: set(subprocess.run(["docker", "ps", "-q"], capture_output=True,
                                             text=True).stdout.split())
        before = running()
        r = self.py("while True: pass", timeout=1)
        self.assertTrue(r.timeout)
        time.sleep(1)
        self.assertEqual(running() - before, set())


if __name__ == "__main__":
    unittest.main()