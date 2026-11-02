"""Micro-benchmark of cogs/db.py per-call cost.

Usage (any cwd; the repo root is derived from this file's location):

    <python> docs/superpowers/plans/perf/db_microbench.py [--n 1000] [--repeats 3]

Runs inside a fresh temp directory containing db/ (db paths are relative and
resolved at call time). Each case is run --repeats times; the reported figure
is the minimum, in microseconds per call.

Cases:
  connect_wal   with db.connect(db.USERS): pass            (WAL file)
  connect_plain with db.connect(db.ATTENDANCE): pass       (default journal)
  run_select    asyncio.run(loop of N x await db.run(USERS, SELECT 1))
"""
import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from cogs import db  # noqa: E402


def bench_connect(path: str, n: int) -> float:
    start = time.perf_counter()
    for _ in range(n):
        with db.connect(path):
            pass
    return (time.perf_counter() - start) / n * 1e6


def bench_run(path: str, n: int) -> float:
    async def loop() -> None:
        for _ in range(n):
            await db.run(path, lambda c: c.execute("SELECT 1").fetchone())

    start = time.perf_counter()
    asyncio.run(loop())
    return (time.perf_counter() - start) / n * 1e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    cases = [
        ("connect_wal", lambda: bench_connect(db.USERS, args.n)),
        ("connect_plain", lambda: bench_connect(db.ATTENDANCE, args.n)),
        ("run_select", lambda: bench_run(db.USERS, args.n)),
    ]
    old = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            os.makedirs("db", exist_ok=True)
            print(f"python: {sys.executable}  n={args.n}  repeats={args.repeats}")
            for name, fn in cases:
                samples = [fn() for _ in range(args.repeats)]
                print(f"{name:14} min={min(samples):8.1f} us/call  "
                      f"all={[round(s, 1) for s in samples]}", flush=True)
        finally:
            os.chdir(old)
    return 0


if __name__ == "__main__":
    sys.exit(main())
