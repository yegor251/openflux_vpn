"""Analytics: JSONL events + the report CLI (no database)."""

import json
from datetime import datetime, timedelta, timezone


def _month_file(tmp_path, events):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    path = tmp_path / f"analytics-{month}.jsonl"
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_emit_appends_jsonl_and_chmods(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLUX_BOT_CONFIG_DIR", str(tmp_path))
    from oflux_bot.analytics import emit

    emit("issue", carrier="cups", tg=1)
    emit("issue", carrier="telemost", tg=2)

    path = next(tmp_path.glob("analytics-*.jsonl"))
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["kind"] == "issue" and lines[0]["carrier"] == "cups"
    assert "ts" in lines[0]
    assert path.stat().st_mode & 0o777 == 0o600


def test_emit_swallows_any_error(monkeypatch):
    monkeypatch.setenv("OFLUX_BOT_CONFIG_DIR", "/proc/definitely/not/writable")
    from oflux_bot.analytics import emit

    emit("issue", carrier="cups")  # must not raise


def test_report_aggregates(tmp_path, capsys):
    from oflux_bot.analytics import report

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _month_file(tmp_path, [
        {"ts": now, "kind": "issue", "carrier": "cups", "tg": 1},
        {"ts": now, "kind": "issue", "carrier": "cups", "tg": 2},
        {"ts": now, "kind": "issue", "carrier": "telemost", "tg": 1},
        {"ts": now, "kind": "switch", "frm": "cups", "to": "telemost", "back": False, "tg": 1},
        {"ts": now, "kind": "rotate", "carrier": "telemost", "tg": 1},
        {"ts": now, "kind": "carrier_failed", "carrier": "cups"},
        {"ts": "2020-01-01T00:00:01+00:00", "kind": "torn-ish", "carrier": "x"},
        {"ts": now, "kind": "carrier_recovered", "carrier": "cups", "stable": False},
        {"ts": now, "kind": "carrier_warning", "carrier": "cups", "users": 2},
        {"ts": now, "kind": "issue_error", "carrier": "cups", "tg": 3, "error": "сессия истекла"},
    ])
    # loader reads per line: append a real torn line to the file
    path = next(tmp_path.glob("analytics-*.jsonl"))
    with open(path, "a", encoding="utf-8") as f:
        f.write("garbage{{{\n")

    report(config_dir=tmp_path, days=30)

    out = capsys.readouterr().out
    assert "issue: 3 (cups 2, telemost 1)" in out
    assert "switch: 1 (cups→telemost x1)" in out
    assert "rotate: 1" in out
    assert "users seen: 3" in out
    assert "carrier cups: 1 failure episode(s), total down" in out
    assert "STILL DOWN" not in out  # recovered closes the episode
    assert "carrier_warning: 1" in out
    assert "errors: 1 (last: issue_error" in out


def test_report_downtime_includes_open_episode(tmp_path, capsys):
    from oflux_bot.analytics import report

    now = datetime.now(timezone.utc)
    failed = (now - timedelta(minutes=90)).isoformat(timespec="seconds")
    _month_file(tmp_path, [
        {"ts": failed, "kind": "carrier_failed", "carrier": "telemost"},
    ])

    report(config_dir=tmp_path, days=30)

    out = capsys.readouterr().out
    assert "carrier telemost: 1 failure episode(s)" in out
    assert "STILL DOWN" in out
    assert "total down 1h" in out  # ~90 min open, formatted 1h30m


def test_report_days_window_filters_old_events(tmp_path, capsys):
    from oflux_bot.analytics import report

    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
    _month_file(tmp_path, [
        {"ts": old, "kind": "issue", "carrier": "cups", "tg": 1},
        {"ts": fresh, "kind": "issue", "carrier": "telemost", "tg": 2},
    ])

    report(config_dir=tmp_path, days=30)
    out = capsys.readouterr().out
    assert "issue: 1 (telemost 1)" in out

    report(config_dir=tmp_path, days=None)  # 0 = all
    out = capsys.readouterr().out
    assert "issue: 2 (cups 1, telemost 1)" in out
