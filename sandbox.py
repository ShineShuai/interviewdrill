"""Run untrusted code with time, CPU, file-size and (optionally) memory limits.

Not a security boundary. Set DRILL_SANDBOX=docker to run generated Python in a container
(no network, 512 MB, 64 pids, read-only, no capabilities). Sanitizer runs pass mem_mb=0: ASan needs a large address space.
"""
import os
import resource
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

MAX_OUT = 1_000_000  # bytes of stdout/stderr kept
BUILD_FSIZE = 1 << 30  # file-size cap for compiler runs: Go -race writes a ~20 MB runtime


@dataclass
class Result:
    rc: int  # exit code; -1 on timeout, 127 if the command could not start
    out: str
    err: str
    secs: float
    timeout: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timeout


def _limits(cpu: int, mem_mb: int, fsize: int):
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if mem_mb and sys.platform == "linux":  # macOS does not enforce RLIMIT_AS
            resource.setrlimit(resource.RLIMIT_AS, (mem_mb << 20, mem_mb << 20))

    return apply


def run(cmd, stdin: str = "", timeout: float = 5.0, cwd=None, mem_mb: int = 512,
        env: dict | None = None, fsize: int = 4 * MAX_OUT) -> Result:
    """Run cmd in its own process group with a minimal environment; output goes to files."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", **(env or {})}
    with tempfile.TemporaryDirectory() as tmp:
        out, err = Path(tmp, "out"), Path(tmp, "err")
        t0 = time.monotonic()
        try:
            with out.open("wb") as o, err.open("wb") as e:
                p = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=o, stderr=e, cwd=cwd, env=env,
                    start_new_session=True, preexec_fn=_limits(int(timeout) + 2, mem_mb, fsize))
                try:
                    p.communicate(stdin.encode(), timeout)
                    rc, timed_out = p.returncode, False
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.communicate()
                    rc, timed_out = -1, True
        except OSError as exc:
            return Result(127, "", str(exc), 0.0)
        secs = time.monotonic() - t0
        text = lambda f: f.read_bytes()[:MAX_OUT].decode(errors="replace")  # noqa: E731
        return Result(rc, text(out), text(err), secs, timed_out)


def run_py(script: Path, *args: str, stdin: str = "", timeout: float = 5.0) -> Result:
    """Run a generated Python script (isolated mode), or in Docker if DRILL_SANDBOX=docker."""
    if os.environ.get("DRILL_SANDBOX") == "docker":
        # Killing the `docker run` client does not stop the container: name it, kill it ourselves.
        name = f"drill-{uuid.uuid4().hex[:12]}"
        cmd = ["docker", "run", "--rm", "-i", "--name", name, "--network", "none",
               "--memory", "512m", "--pids-limit", "64", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp",
               "-v", f"{script.parent.resolve()}:/w:ro", "-w", "/w",
               "python:3.12-slim", "python", "-I", script.name, *args]
        result = run(cmd, stdin, timeout + 10, mem_mb=0)
        if result.timeout:
            subprocess.run(["docker", "kill", name], capture_output=True, check=False, timeout=30)
        return result
    return run([sys.executable, "-I", str(script), *args], stdin, timeout)
