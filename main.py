#!/usr/bin/env python3
"""
DAV University CSE - Timetable Generator (CP-SAT)

Usage:
    python main.py --input teaching_load.xlsx --output timetable.xlsx
    python main.py --input teaching_load.xlsx --output timetable.xlsx --time-limit 120 --no-balance

Re-run this any time the teaching-load register changes -- the
timetable is rebuilt from scratch from whatever file you point it at.
"""

from __future__ import annotations

import argparse
import sys
import time

from timetable_scheduler import (
    parse_teaching_load, build_sessions, build_and_solve, export_workbook,
)
from timetable_scheduler.config import (
    DEFAULT_TIME_LIMIT_SECONDS, DEFAULT_NUM_WORKERS, ROOMS, DAYS, PERIODS_PER_DAY,
)


def main():
    ap = argparse.ArgumentParser(description="Generate a CSE department timetable from a teaching-load register.")
    ap.add_argument("--input", "-i", required=True, help="Path to teaching-load .xlsx or .csv")
    ap.add_argument("--output", "-o", default="timetable_output.xlsx", help="Path to write the output .xlsx")
    ap.add_argument("--time-limit", type=int, default=DEFAULT_TIME_LIMIT_SECONDS,
                     help=f"Solver time limit in seconds (default {DEFAULT_TIME_LIMIT_SECONDS})")
    ap.add_argument("--workers", type=int, default=DEFAULT_NUM_WORKERS,
                     help=f"CP-SAT parallel search workers (default {DEFAULT_NUM_WORKERS})")
    ap.add_argument("--no-balance", action="store_true",
                     help="Skip the load-balancing objective and just find any feasible timetable (faster)")
    ap.add_argument("--verbose", action="store_true", help="Print CP-SAT search log")
    args = ap.parse_args()

    print(f"[1/4] Parsing teaching load from {args.input} ...")
    offerings = parse_teaching_load(args.input)
    print(f"      {len(offerings)} course-offering rows across "
          f"{len({o.faculty for o in offerings})} faculty, "
          f"{len({o.section for o in offerings})} sections.")

    sessions = build_sessions(offerings)
    print(f"[2/4] Expanded to {len(sessions)} weekly sessions "
          f"(theory/tutorial = 1 period, practicals grouped into continuous blocks).")

    print(f"[3/4] Solving with OR-Tools CP-SAT "
          f"({len(ROOMS)} rooms x {len(DAYS)} days x {PERIODS_PER_DAY} periods, "
          f"time limit {args.time_limit}s, {args.workers} workers, "
          f"balance={'off' if args.no_balance else 'on'}) ...")
    t0 = time.time()
    result = build_and_solve(
        sessions,
        time_limit_seconds=args.time_limit,
        num_workers=args.workers,
        balance_load=not args.no_balance,
        log_progress=args.verbose,
    )
    print(f"      status={result.status}  solve_time={result.wall_time_seconds:.1f}s")

    if result.status in ("INFEASIBLE", "UNKNOWN") and not result.placed:
        print("\n No timetable could be produced at all.")
        if result.status == "UNKNOWN":
            print("   The solver did not finish within the time limit. Try increasing --time-limit.")
        else:
            print("   No valid room/period combination exists for this load and configuration --")
            print("   check the rooms/days/periods in config.py against the teaching load.")
        sys.exit(1)

    required = sum(p.session.duration for p in result.placed) + sum(s.duration for s in result.unplaced)
    scheduled = sum(p.session.duration for p in result.placed)
    if result.unplaced:
        pct = 100.0 * scheduled / required if required else 100.0
        print(f"\n  COMPLETE TIMETABLE COULD NOT BE FOUND ({result.status}).")
        print(f"  Best-effort timetable: {scheduled}/{required} periods scheduled ({pct:.1f}%).")
        print(f"  {len(result.unplaced)} session(s) could not be placed:")
        for s in result.unplaced[:10]:
            print(f"      - {s.course_code} ({s.teacher} / {s.section}, {s.kind})")
        if len(result.unplaced) > 10:
            print(f"      ... and {len(result.unplaced) - 10} more")
    else:
        print(f"\n  All {len(result.placed)} sessions placed with zero teacher/section/room clashes.")

    print(f"[4/4] Writing {args.output} ...")
    export_workbook(result, args.output)
    print("Done.")


if __name__ == "__main__":
    main()
