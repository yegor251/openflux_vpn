#!/usr/bin/env python3
"""Refill the cups.online room pool on the VPS (run on the ADMIN machine).

cups.online walls room CREATION from datacenter-ish automated clients after
a volume (403), roughly one burst of 8 rooms per cooldown window — so the
script pushes every set the moment it is minted and stops gracefully on the
first refusal. 8 rooms = one channel.

Usage:
  .venv/bin/python scripts/refill_cups_pool.py [sets]   # default: 1

Pool: /root/.config/oflux-bot/cups-pool.json  {"sets": ["<packed>", ...]}
Watch the count the script prints — /issue fails loudly when it hits 0.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from docprovider.cupsrooms import CupsRoomError, create_rooms, pack_rooms

VPS = "root@159.194.227.248"
POOL = "/root/.config/oflux-bot/cups-pool.json"


def push_sets(new_sets: list[str]) -> int:
    # base64 payload: no quoting games over ssh (the sets are opaque strings)
    payload = base64.b64encode(json.dumps(new_sets).encode()).decode()
    remote = (
        "python3 -c \"import json,base64,pathlib; "
        f"new=json.loads(base64.b64decode('{payload}')); "
        f"p=pathlib.Path('{POOL}'); "
        "d=json.loads(p.read_text()) if p.exists() else {'sets': []}; "
        "d['sets']=new+d['sets']; "
        "p.write_text(json.dumps(d,indent=1)); p.chmod(0o600); "
        "print(len(d['sets']))\""
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", VPS, remote], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise SystemExit(f"push failed: {proc.stderr.strip()[:300]}")
    return int(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sets", nargs="?", type=int, default=1, help="room sets to mint (8 rooms each)")
    args = ap.parse_args()

    pushed = 0
    for i in range(args.sets):
        try:
            rooms = pack_rooms(create_rooms())
        except CupsRoomError as exc:
            print(f"сет {i + 1}: сервис отказал ({exc}).")
            print("лимит создания исчерпан — повтори позже (обычно отпускает в течение часа)")
            break
        total = push_sets([rooms])
        pushed += 1
        print(f"сет {i + 1}/{args.sets}: 8 комнат → пул (в пуле сейчас {total})")

    if pushed == 0:
        print("пул не пополнен")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
