#!/usr/bin/env python3
"""Refill the cups.online room pool on the VPS (run on the ADMIN machine).

cups.online walls room CREATION from datacenter IPs (403 after volume),
but the admin machine is fine — so rooms are minted here and pushed into
the VPS pool the bot's /issue pops from. 8 rooms = one channel.

Usage:
  .venv/bin/python scripts/refill_cups_pool.py [sets]   # default: 2

Pool: /root/.config/oflux-bot/cups-pool.json  {"sets": ["<packed>", ...]}
Watch the count the script prints — /issue fails loudly when it hits 0.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from docprovider.cupsrooms import create_rooms, pack_rooms

VPS = "root@159.194.227.248"
POOL = "/root/.config/oflux-bot/cups-pool.json"


def push_sets(new_sets: list[str]) -> int:
    quoted = json.dumps(new_sets)
    remote = (
        f"python3 -c \"import json,pathlib; p=pathlib.Path('{POOL}'); "
        f"d=json.loads(p.read_text()) if p.exists() else {{'sets': []}}; "
        f"d['sets'] = {quoted} + d.get('sets', []); "
        f"p.write_text(json.dumps(d, indent=1)); p.chmod(0o600); "
        f"print(len(d['sets']))\""
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", VPS, remote], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise SystemExit(f"push failed: {proc.stderr.strip()[:200]}")
    return int(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sets", nargs="?", type=int, default=2, help="room sets to mint (8 rooms each)")
    args = ap.parse_args()

    new_sets = []
    for i in range(args.sets):
        rooms = pack_rooms(create_rooms())
        new_sets.append(rooms)
        print(f"set {i + 1}/{args.sets}: 8 rooms packed")
    total = push_sets(new_sets)
    print(f"пул на VPS: {total} сет(а) — хватает на {total} канал(ов)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
