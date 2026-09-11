#!/usr/bin/env python3
"""Generate a realistic sample log CSV for trying csvopt out.

    python tools/make_sample_log.py sample.csv --rows 2000000
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import datetime, timedelta

LEVELS = (["INFO"] * 70) + (["DEBUG"] * 15) + (["WARN"] * 10) + (["ERROR"] * 4) + ["FATAL"]
SERVICES = ["auth", "api-gateway", "billing", "search", "worker", "notifier", "db-proxy"]
MESSAGES = [
    "request completed",
    "cache miss for key user:{id}",
    "slow query detected",
    "connection reset by peer",
    "retrying upstream call",
    "payload validation failed",
    "user session expired",
    "배치 작업 완료",  # non-ASCII on purpose: encoding handling gets exercised
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--encoding", default="utf-8")
    ap.add_argument("--newline", choices=["lf", "crlf"], default="lf")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    start = datetime(2026, 1, 1, 0, 0, 0)
    term = "\r\n" if args.newline == "crlf" else "\n"
    with open(args.path, "w", encoding=args.encoding, newline="") as fh:
        w = csv.writer(fh, lineterminator=term)
        w.writerow(["ts", "level", "service", "host", "latency_ms", "status", "user_id", "message"])
        for i in range(args.rows):
            ts = start + timedelta(milliseconds=i * rnd.randint(1, 40))
            level = rnd.choice(LEVELS)
            status = rnd.choice([200, 200, 200, 201, 301, 400, 404, 500, 503])
            msg = rnd.choice(MESSAGES).format(id=rnd.randint(1, 99999))
            if level in ("ERROR", "FATAL") and rnd.random() < 0.2:
                msg = f'{msg}, trace="line1\nline2"'  # embedded newline + comma
            w.writerow([
                ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                level,
                rnd.choice(SERVICES),
                f"host-{rnd.randint(1, 40):03d}",
                round(rnd.lognormvariate(3.0, 1.0), 1),
                status,
                rnd.randint(1, 99999),
                msg,
            ])
    print(f"wrote {args.rows:,} rows to {args.path}")


if __name__ == "__main__":
    main()
