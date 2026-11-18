import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
