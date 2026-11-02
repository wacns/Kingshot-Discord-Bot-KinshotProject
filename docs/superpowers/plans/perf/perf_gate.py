"""Database perf gate: pytest-reported session time for the six perf-set files.

Usage (from anywhere; the repo root is derived from this file's location):

    <python> docs/superpowers/plans/perf/perf_gate.py [--runs 7] [--out result.json]
    <python> docs/superpowers/plans/perf/perf_gate.py --compare docs/superpowers/plans/perf/baseline.json

<python> is the interpreter that runs the test suite; it is sys.executable.

Per file it runs `<python> -m pytest <file> -q -p no:cacheprovider` --runs times
back to back and parses the session time from pytest's own summary line
("N passed[, M warning] in X.XXs"). The gate statistic is the MINIMUM over the runs.

--compare BASELINE: a file FAILS when its minimum is slower than the baseline
minimum by more than max(10% of the baseline, 0.15 s), or when its pass count
differs from the baseline. Exit status is 1 if any file fails.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
FILES = [
    "tests/test_gift_redeem_accounting.py",
    "tests/test_gift_redeem_resume.py",
    "tests/test_process_queue_reload.py",
    "tests/test_bear_persist_no_deadlock.py",
    "tests/test_minister_booking.py",
    "tests/test_notification_fire_path.py",
]
SESSION_RE = re.compile(r"\bin ([0-9]+(?:\.[0-9]+)?)s\b")
PASSED_RE = re.compile(r"(\d+) passed")
FAILED_RE = re.compile(r"(\d+) failed")
REL_TOL = 0.10
ABS_TOL = 0.15


def run_once(py: str, test_file: str) -> dict:
    cmd = [py, "-m", "pytest", test_file, "-q", "-p", "no:cacheprovider"]
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    text = (p.stdout or "") + (p.stderr or "")
    summary = next((l for l in reversed(text.splitlines()) if SESSION_RE.search(l)), "")
    session = SESSION_RE.search(summary)
    passed = PASSED_RE.search(summary)
    failed = FAILED_RE.search(summary)
    return {
        "seconds": float(session.group(1)) if session else None,
        "passed": int(passed.group(1)) if passed else 0,
        "failed": int(failed.group(1)) if failed else 0,
        "exit": p.returncode,
        "summary": summary.strip(),
    }


def measure(py: str, runs: int) -> dict:
    results = {}
    for f in FILES:
        rs = [run_once(py, f) for _ in range(runs)]
        secs = [r["seconds"] for r in rs]
        valid = [s for s in secs if s is not None]
        results[f] = {
            "runs": secs,
            "min": min(valid) if valid else None,
            "passed": rs[0]["passed"],
            "failed": max(r["failed"] for r in rs),
            "exit_codes": [r["exit"] for r in rs],
            "pass_counts_consistent": len({r["passed"] for r in rs}) == 1,
            "summaries": sorted({r["summary"] for r in rs}),
        }
        print(f"{f}: min={results[f]['min']} passed={results[f]['passed']} "
              f"runs={secs}", flush=True)
    return results


def compare(current: dict, baseline: dict) -> bool:
    ok = True
    print(f"{'file':44} {'base min':>9} {'cur min':>9} {'delta':>8} {'limit':>7}  verdict")
    for f in FILES:
        b = baseline["files"][f]
        c = current[f]
        limit = max(REL_TOL * b["min"], ABS_TOL)
        delta = c["min"] - b["min"]
        reasons = []
        if c["min"] is None:
            reasons.append("no pytest session time parsed")
            delta = float("nan")
        elif delta > limit:
            reasons.append(f"slower by {delta:.3f}s > {limit:.3f}s")
        if c["passed"] != b["passed"]:
            reasons.append(f"passed {b['passed']} -> {c['passed']}")
        verdict = "FAIL (" + "; ".join(reasons) + ")" if reasons else "ok"
        ok &= not reasons
        cur = "n/a" if c["min"] is None else f"{c['min']:.3f}"
        print(f"{f:44} {b['min']:9.3f} {cur:>9} {delta:+8.3f} {limit:7.3f}  {verdict}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--out", help="write results JSON here")
    ap.add_argument("--compare", help="baseline JSON written by --out")
    args = ap.parse_args()

    py = sys.executable
    print(f"python: {py}  repo: {REPO}  runs per file: {args.runs}", flush=True)
    files = measure(py, args.runs)
    doc = {"python": py, "runs_per_file": args.runs,
           "command": "<python> -m pytest <file> -q -p no:cacheprovider",
           "files": files}
    if args.out:
        Path(args.out).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    if args.compare:
        baseline = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        return 0 if compare(files, baseline) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
