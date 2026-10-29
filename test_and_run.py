"""Run hospital project baseline tests and critical regression tests.

Place this file and test_hospital_regressions.py next to app.py, then run:
    python test_and_run.py
All output is saved to test_results.txt in the same folder.
"""
from __future__ import annotations

import datetime
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "test_results.txt"


def main() -> int:
    print("Hospital Digital Twin - Complete Test Runner")
    if not (ROOT / "app.py").is_file() or not (ROOT / "tests").is_dir():
        print("ERROR: Put both test_and_run.py and test_hospital_regressions.py in the project folder containing app.py and tests/.")
        return 2
    if not (ROOT / "test_hospital_regressions.py").is_file():
        print("ERROR: test_hospital_regressions.py must be in the same folder as test_and_run.py.")
        return 2

    tasks = [
        ("EXISTING PROJECT TESTS", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_[!h]*.py", "-v"]),
        ("CRITICAL REGRESSION TESTS (expected to expose 4 current defects)", [sys.executable, "test_hospital_regressions.py", "-v"]),
    ]
    statuses = []
    with LOG.open("w", encoding="utf-8") as f:
        def write(line: str) -> None:
            print(line, flush=True)
            f.write(line + "\n")
            f.flush()

        write("Hospital Digital Twin - Test Results")
        write("Date: " + datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
        write("Python: " + sys.version.replace("\n", " "))
        write("Platform: " + platform.platform())
        write("Project folder: " + str(ROOT))
        write("Note: FAIL/ERROR in the new regression suite are expected on the current prototype.")

        for title, args in tasks:
            write("\n" + "=" * 68)
            write(title)
            write("=" * 68)
            try:
                run = subprocess.run(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=180)
                write(run.stdout.rstrip("\n"))
                statuses.append((title, run.returncode))
            except subprocess.TimeoutExpired:
                write("TIMEOUT: test run exceeded 180 seconds")
                statuses.append((title, 124))
            except Exception as exc:
                write(f"ERROR running tests: {type(exc).__name__}: {exc}")
                statuses.append((title, 125))

        write("\n" + "=" * 68)
        write("SUMMARY")
        for title, code in statuses:
            write(f"- {title}: {'PASS' if code == 0 else f'FAIL/ERROR (exit code {code})'}")
        write("=" * 68)
        write("Send this entire test_results.txt file back for review.")
    print("\nReport saved at:", LOG)
    return 0 if all(code == 0 for _, code in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
