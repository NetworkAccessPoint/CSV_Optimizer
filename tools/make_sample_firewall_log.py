#!/usr/bin/env python3
"""Generate a sample firewall log for trying the multi-condition searcher out.

    python tools/make_sample_firewall_log.py fw.csv --rows 500000
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import random
from datetime import datetime, timedelta

# Hosts that the sample policy actually talks about.
MANAGED = ["105.181.150.6", "105.181.150.7"]
SUBNETS = ["105.181.155.0/24", "105.181.156.0/24", "105.182.71.0/24", "105.182.62.0/24"]
NMS = "70.222.12.0/26"          # the management network
SYSLOG = "70.222.12.62"
NOISE = ["10.14.3.0/24", "192.168.40.0/24", "172.20.8.0/24"]


def host_in(cidr: str, rnd: random.Random) -> str:
    net = ipaddress.ip_network(cidr)
    size = net.num_addresses
    return str(net.network_address + rnd.randrange(1, max(2, size - 1)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    ap.add_argument("--rows", type=int, default=500_000)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    start = datetime(2026, 3, 1)
    with open(args.path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "src_ip", "dst_ip", "protocol", "src_port",
                    "dst_port", "action", "bytes"])
        for i in range(args.rows):
            ts = start + timedelta(milliseconds=i * rnd.randint(1, 60))
            kind = rnd.random()
            if kind < 0.22:                       # SNMP poll from the NMS
                src, dst, proto, dport = host_in(NMS, rnd), rnd.choice(MANAGED), "UDP", 161
            elif kind < 0.36:                     # syslog back to the collector
                src, dst, proto, dport = rnd.choice(MANAGED), SYSLOG, "UDP", 514
            elif kind < 0.46:                     # SNMP trap
                src, dst, proto, dport = rnd.choice(MANAGED), host_in(NMS, rnd), "UDP", 162
            elif kind < 0.54:                     # ping
                src, dst, proto, dport = host_in(NMS, rnd), rnd.choice(MANAGED), "ICMP", 8
            elif kind < 0.62:                     # config backup
                src, dst, proto, dport = (host_in(NMS, rnd), rnd.choice(MANAGED), "TCP",
                                          rnd.choice([22, 23]))
            elif kind < 0.78:                     # the same, against the subnets
                target = rnd.choice(SUBNETS)
                src, dst, proto, dport = (host_in(NMS, rnd), host_in(target, rnd), "UDP", 161)
            else:                                 # traffic no policy row covers
                src = host_in(rnd.choice(NOISE), rnd)
                dst = host_in(rnd.choice(NOISE + SUBNETS), rnd)
                proto = rnd.choice(["TCP", "UDP"])
                dport = rnd.choice([80, 443, 445, 3389, 8080])
            w.writerow([
                ts.strftime("%Y-%m-%d %H:%M:%S"), src, dst, proto,
                rnd.randint(1024, 65535), dport,
                "allow" if rnd.random() < 0.93 else "deny",
                rnd.randint(64, 20000),
            ])
    print(f"wrote {args.rows:,} rows to {args.path}")


if __name__ == "__main__":
    main()
