"""Bot analytics without a database: JSONL events + a report CLI.

emit(kind, **fields) appends one line {"ts", "kind", **fields} to
analytics-<YYYY-MM>.jsonl in the bot's config dir (0600). Never raises:
analytics decorates live bot paths and must not break them.

Events (emitted where the decision happens):
  issue / issue_error            carrier=..., tg=...
  switch / switch_error          frm=..., to=..., back=..., tg=...
  rotate / rotate_error          carrier=..., tg=...
  revoke                         carrier=..., tg=...
  cmd                            cmd=...            (/start menu buttons)
  carrier_failed / recovered / unstable            carrier=...
  carrier_warning                carrier=..., users=N
  channel_warning                carrier=..., tg=...

The admin reads it through the agent: `python -m oflux_bot.analytics
--days 30` on the VPS (or ask the agent to run and explain it).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import bot_config_dir


def emit(kind: str, **fields) -> None:
    """Append one event; swallow every error (must never break the bot)."""
    try:
        d = bot_config_dir()
        d.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        path = d / f"analytics-{now.strftime('%Y-%m')}.jsonl"
        line = json.dumps(
            {"ts": now.isoformat(timespec="seconds"), "kind": kind, **fields},
            ensure_ascii=False,
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        path.chmod(0o600)
    except Exception:  # noqa: BLE001 - analytics is best-effort by design
        pass


def _load_events(config_dir: Path | None) -> list[dict]:
    d = Path(config_dir) if config_dir else bot_config_dir()
    events: list[dict] = []
    for path in sorted(d.glob("analytics-*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # torn line (a crash mid-append)
    return events


def _downtime(events: list[dict]) -> dict[str, tuple[int, float, bool]]:
    """Per carrier: (episodes, total down seconds, still-down-now). An open
    episode counts up to NOW (the report moment)."""
    state: dict[str, str] = {}
    opened: dict[str, datetime] = {}
    episodes: dict[str, int] = {}
    total: dict[str, float] = {}
    for e in events:
        ts = _ts(e)
        kind = e.get("carrier", "?")
        if e["kind"] == "carrier_failed":
            state[kind] = "down"
            opened[kind] = ts
            episodes[kind] = episodes.get(kind, 0) + 1
        elif e["kind"] == "carrier_recovered" and state.get(kind) == "down":
            total[kind] = total.get(kind, 0.0) + (ts - opened[kind]).total_seconds()
            state[kind] = "up"
    out: dict[str, tuple[int, float, bool]] = {}
    for k in set(episodes) | set(state):
        still = state.get(k) == "down"
        secs = total.get(k, 0.0)
        if still and k in opened:
            secs += max(0.0, (datetime.now(timezone.utc) - opened[k]).total_seconds())
        out[k] = (episodes.get(k, 0), secs, still)
    return out


def _ts(e: dict) -> datetime:
    try:
        return datetime.fromisoformat(e["ts"])
    except (KeyError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _fmt_h(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def report(config_dir=None, days: int | None = 30, stream=None) -> None:
    """Print the human-readable summary; the agent is the intended reader."""
    out = stream or sys.stdout
    events = _load_events(config_dir)
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        events = [e for e in events if _ts(e) >= cutoff]

    def n(kind: str) -> int:
        return sum(1 for e in events if e["kind"] == kind)

    def users_of(*kinds: str) -> int:
        return len({e.get("tg") for e in events if e["kind"] in kinds and e.get("tg")})

    print(f"Analytics (last {days or 'all'} days): {len(events)} events")
    if not events:
        print("  (nothing collected yet)")
        return

    tgs = {e.get("tg") for e in events if e.get("tg")}
    print(f"users seen: {len(tgs)}")

    issues = [e for e in events if e["kind"] == "issue"]
    if issues:
        per: dict[str, int] = {}
        for e in issues:
            per[e.get("carrier", "?")] = per.get(e.get("carrier", "?"), 0) + 1
        detail = ", ".join(f"{k} {v}" for k, v in sorted(per.items()))
        print(f"issue: {len(issues)} ({detail})")

    switches = [e for e in events if e["kind"] == "switch"]
    if switches:
        pairs: dict[str, int] = {}
        for e in switches:
            key = f"{e.get('frm', '?')}→{e.get('to', '?')}"
            if e.get("back"):
                key += " (back)"
            pairs[key] = pairs.get(key, 0) + 1
        detail = ", ".join(f"{k} x{v}" for k, v in sorted(pairs.items()))
        print(f"switch: {len(switches)} ({detail})")

    for kind, label in (("rotate", "rotate"), ("revoke", "revoke"), ("cmd", "menu buttons")):
        count = n(kind)
        if count:
            print(f"{label}: {count}")

    downs = _downtime(events)
    for carrier, (eps, secs, still) in sorted(downs.items()):
        mark = " (STILL DOWN)" if still else ""
        print(f"carrier {carrier}: {eps} failure episode(s), total down {_fmt_h(secs)}{mark}")

    for kind in ("carrier_warning", "channel_warning"):
        count = n(kind)
        if count:
            print(f"{kind}: {count}")

    errors = [e for e in events if e["kind"].endswith("_error")]
    if errors:
        last = errors[-1]
        print(f"errors: {len(errors)} (last: {last['kind']} — {str(last.get('error'))[:100]})")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Print bot analytics from JSONL events")
    ap.add_argument("--days", type=int, default=30, help="look-back window, 0 = all (default 30)")
    ap.add_argument("--config-dir", default=None, help="override the bot config dir")
    args = ap.parse_args()
    report(config_dir=args.config_dir, days=args.days or None)
