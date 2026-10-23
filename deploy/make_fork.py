#!/usr/bin/env python3
"""Generate our node-install.sh fork from a fresh upstream copy.

Two supported lines (auto-detected):

- **team line** (`OpenFluxTeam/OpenFlux`, our production source since
  2026-10-23): ships `mode=`, `YANDEX=`, `COOKIES=`, l3 drop-ins and an
  RST oneshot natively. The ONLY thing it gets wrong is the RST rule
  order: it appends (`iptables -A`), which under ufw never sees the
  packets (the built-in chains ACCEPT ctstate NEW first) — our live
  finding of 2026-10-18. One patch: insert first.
- **main line** (`p1neappleXpress/OpenFlux`): everything below in
  PATCHES.

This script re-applies the patches to a freshly updated
`OpenFlux/deploy/node-install.sh`, so upstream changes are picked up
and — more importantly — upstream text drift fails loudly here instead
of breaking channel provisioning on a server.

Usage: python3 deploy/make_fork.py [--check]
  --check  verify the current fork is up to date without writing
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = ROOT / "OpenFlux" / "deploy" / "node-install.sh"
FORK = ROOT / "deploy" / "node-install.sh"

# (id, old, new) — every entry must apply exactly once
PATCHES = [
    (
        "header-note",
        "#!/bin/sh\n# Managed by OpenFlux node-install.sh",
        "#!/bin/sh\n# Managed by OpenFlux node-install.sh\n"
        "#\n"
        "# FORK (openflux_vpn): adds the `yandex=` carrier key (the plain\n"
        "# Yandex.Docs WebSocket transport, anonymous, PoW auto-solved) and the\n"
        "# `mode=` key (l3 with kernel-RST suppression) on top of the stock\n"
        "# vyandex=/mailru=/cupsonline=. Everything else is upstream; the\n"
        "# updater runs this fork via install_self.",
    ),
    (
        "declare-var",
        'CHANNEL=""; URL=""; MAILRU=""; CUPS=""; KEY=""; PORT=""; AUTOUPDATE=""',
        'CHANNEL=""; YANDEX=""; MODE=""; URL=""; MAILRU=""; CUPS=""; KEY=""; PORT=""; AUTOUPDATE=""',
    ),
    (
        "parse-key",
        "            channel=*) CHANNEL=${line#channel=} ;;\n            url=*) URL=${line#url=} ;;",
        "            channel=*) CHANNEL=${line#channel=} ;;\n"
        "            yandex=*) YANDEX=${line#yandex=} ;;\n"
        "            mode=*) MODE=${line#mode=} ;;\n"
        "            url=*) URL=${line#url=} ;;",
    ),
    (
        "validate-key",
        '    [ -z "$URL" ] || valid_url "$URL" || fail input "адрес документа должен быть вида https://docs.yandex.ru/edit/d/..."',
        '    [ -z "$YANDEX" ] || valid_url "$YANDEX" || fail input "адрес документа должен быть вида https://docs.yandex.ru/edit/d/..."\n'
        '    [ -z "$URL" ] || valid_url "$URL" || fail input "адрес документа должен быть вида https://docs.yandex.ru/edit/d/..."\n'
        '    case "$MODE" in ""|l3|l4) ;; *) fail input "mode: l3 или l4" ;; esac',
    ),
    (
        "transport-names",
        '    names=""\n    [ -n "$URL" ] && names="Яндекс Документ"',
        '    names=""\n    [ -n "$YANDEX" ] && names="Яндекс Документ (WS)"\n'
        '    [ -z "$names" ] && [ -n "$URL" ] && names="Яндекс Документ"',
    ),
    (
        "session-context",
        "session_context() {\n    if [ -n \"$URL\" ]; then printf '%s' \"$URL\"; elif [ -n \"$MAILRU\" ]; then printf '%s' \"$MAILRU\"; fi\n}",
        "session_context() {\n"
        "    if [ -n \"$YANDEX\" ]; then printf '%s' \"$YANDEX\"\n"
        "    elif [ -n \"$URL\" ]; then printf '%s' \"$URL\"\n"
        "    elif [ -n \"$MAILRU\" ]; then printf '%s' \"$MAILRU\"\n"
        "    fi\n}",
    ),
    (
        "node-conf-block",
        '    [ -n "$URL" ] && printf \'\\n[Transport vyandex]\\nType = vyandex\\nPriority = 100\\nURL = %s\\n\' "$URL"',
        '    [ -n "$YANDEX" ] && printf \'\\n[Transport yandex]\\nType = yandex\\nPriority = 100\\nURL = %s\\n\' "$YANDEX"\n'
        '    [ -n "$URL" ] && printf \'\\n[Transport vyandex]\\nType = vyandex\\nPriority = 100\\nURL = %s\\n\' "$URL"',
    ),
    (
        "node-conf-mode",
        "Role = exit\nMode = l4",
        "Role = exit\nMode = ${MODE:-l4}",
    ),
    (
        # l3 (SNAT/DNAT) extras beyond the stock l4 flow:
        #  1. the unit runs as openflux-node; raw sockets need CAP_NET_RAW ->
        #     per-channel drop-in with AmbientCapabilities (works with the
        #     unit's NoNewPrivileges=true);
        #  2. the kernel RSTs return packets for connections it never opened
        #     and tears the tunnel down -> idempotent iptables rule dropping
        #     locally generated RSTs sourced from the egress IP
        #     (architecture.md, "l3 and kernel RSTs"), re-added at boot by a
        #     oneshot unit. Host-wide and deliberately left in place by
        #     remove: an extra RST drop is harmless.
        "l3-support",
        '    [ -n "$CREATED_FW" ] && printf \'%s %s\\n\' "$CREATED_FW" "$PORT" > "$dir/firewall"',
        '    [ -n "$CREATED_FW" ] && printf \'%s %s\\n\' "$CREATED_FW" "$PORT" > "$dir/firewall"\n'
        '\n'
        '    if [ "$MODE" = "l3" ]; then\n'
        '        mkdir -p "/etc/systemd/system/openflux-node@$CHANNEL.service.d"\n'
        '        cat > "/etc/systemd/system/openflux-node@$CHANNEL.service.d/l3.conf" <<EOF\n'
        '$MARKER\n'
        '# mode=l3: raw sockets need CAP_NET_RAW (works with NoNewPrivileges)\n'
        '[Service]\n'
        'AmbientCapabilities=CAP_NET_RAW\n'
        'CapabilityBoundingSet=CAP_NET_RAW\n'
        'EOF\n'
        '        egress=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n \'s/.* src \\([0-9.]*\\).*/\\1/p\' | head -1)\n'
        '        [ -n "$egress" ] || apply_fail firewall "mode=l3: не удалось определить egress IP"\n'
        '        # insert FIRST: ufw-track-output ACCEPTs ctstate NEW before any\n'
        '        # appended rule, so -A here would never see the RSTs\n'
        '        iptables -C OUTPUT -p tcp --tcp-flags RST RST -s "$egress" -j DROP 2>/dev/null \\\n'
        '            || iptables -I OUTPUT 1 -p tcp --tcp-flags RST RST -s "$egress" -j DROP \\\n'
        '            || apply_fail firewall "не удалось добавить iptables-правило против kernel-RST"\n'
        "        cat > /etc/systemd/system/openflux-rstdrop.service <<'EOF'\n"
        '[Unit]\n'
        'Description=openflux l3: suppress kernel RSTs that kill the tunnel\n'
        'After=network-online.target\n'
        '\n'
        '[Service]\n'
        'Type=oneshot\n'
        "ExecStart=/bin/sh -c 'e=$(ip -4 route get 1.1.1.1 | sed -n \"s/.* src \\\\([0-9.]*\\\\).*/\\\\1/p\" | head -1); iptables -C OUTPUT -p tcp --tcp-flags RST RST -s \"$e\" -j DROP 2>/dev/null || iptables -I OUTPUT 1 -p tcp --tcp-flags RST RST -s \"$e\" -j DROP'\n"
        '\n'
        '[Install]\n'
        'WantedBy=multi-user.target\n'
        'EOF\n'
        '        systemctl daemon-reload\n'
        '        systemctl enable openflux-rstdrop.service >/dev/null 2>&1 || true\n'
        '    fi',
    ),
]

PROBES = (
    "FORK (openflux_vpn)",
    "yandex=*) YANDEX=",
    "mode=*) MODE=",
    'case "$MODE" in ""|l3|l4)',
    'Mode = ${MODE:-l4}',
    '[ "$MODE" = "l3" ]',
    "AmbientCapabilities=CAP_NET_RAW",
    "openflux-rstdrop.service",
    "Яндекс Документ (WS)",
    "[Transport yandex]",
)

# The team line (OpenFluxTeam/OpenFlux, 0.14+/node-v1.7+) ships our old
# fork features natively; the single divergence is the RST rule order.
TEAM_PROBE = "write_l3_helper"
TEAM_PATCHES = [
    (
        "rst-insert-first",
        "    add) iptables -C $rule 2>/dev/null || iptables -A $rule ;;",
        "    # insert FIRST: under ufw the built-in OUTPUT chains ACCEPT\n"
        "    # ctstate NEW before any appended rule, so -A never sees the RSTs\n"
        '    add) iptables -C $rule 2>/dev/null || iptables -I OUTPUT 1 -p tcp --tcp-flags RST RST -s "$ip" -j DROP ;;',
    ),
]
TEAM_PROBES = (
    "iptables -I OUTPUT 1 -p tcp --tcp-flags RST RST",
    "[Transport yandex]",
    "AmbientCapabilities=CAP_NET_RAW",
    "yandex=*) YANDEX=",
    "Mode = ${MODE:-l4}",
)


def build_fork(upstream_text: str) -> str:
    if TEAM_PROBE in upstream_text:
        patches, probes = TEAM_PATCHES, TEAM_PROBES
    else:
        patches, probes = PATCHES, PROBES
    text = upstream_text
    for pid, old, new in patches:
        count = text.count(old)
        if count != 1:
            sys.exit(
                f"FORK DRIFT: patch {pid!r} matches {count} times (need 1). "
                "Upstream node-install.sh changed — rework deploy/make_fork.py."
            )
        text = text.replace(old, new, 1)
    for probe in probes:
        assert probe in text, f"post-check failed: {probe!r} missing"
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="only verify, do not write")
    args = parser.parse_args()

    upstream = UPSTREAM.read_text(encoding="utf-8")
    forked = build_fork(upstream)

    if args.check:
        if FORK.exists() and FORK.read_text(encoding="utf-8") == forked:
            print("fork is up to date")
            return 0
        print("fork is STALE: run deploy/make_fork.py after updating OpenFlux/")
        return 1

    tmp = FORK.with_suffix(".sh.tmp")
    tmp.write_text(forked, encoding="utf-8")
    subprocess.run(["sh", "-n", str(tmp)], check=True)  # syntax gate
    tmp.replace(FORK)
    print(f"fork written: {FORK}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
