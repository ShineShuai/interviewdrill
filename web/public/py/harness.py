"""Run one program against one stdin text.

Loaded by the Pyodide worker (web/public/py/worker.js) and by test_harness.py, so the
browser and the tests execute exactly the same code.
"""
import io
import json
import sys
import traceback


def _exit_code(code, err) -> int:
    if code is None:
        return 0
    if isinstance(code, int):  # bool is an int too
        return int(code)
    print(code, file=err)  # sys.exit("message") prints it and exits with status 1
    return 1


def run_case(code: str, stdin_text: str) -> str:
    """Execute `code` as __main__ with the given stdin; returns JSON {rc, out, err}."""
    out, err = io.StringIO(), io.StringIO()
    saved = (sys.stdin, sys.stdout, sys.stderr, sys.getrecursionlimit())
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(stdin_text), out, err
    rc = 0
    try:
        exec(compile(code, "solution.py", "exec"), {"__name__": "__main__"})
    except SystemExit as exc:
        rc = _exit_code(exc.code, err)
    except BaseException:  # noqa: BLE001 - report every failure of the user's program
        traceback.print_exc(file=err)
        rc = 1
    finally:
        sys.stdin, sys.stdout, sys.stderr = saved[:3]
        sys.setrecursionlimit(saved[3])  # a program may raise it; do not leak into the next run
    return json.dumps({"rc": rc, "out": out.getvalue(), "err": err.getvalue()})
