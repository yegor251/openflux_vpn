#!/usr/bin/env python3
"""Generate our node-install.sh fork from a fresh upstream copy.

Our fork adds the `yandex=` carrier key (plain Yandex.Docs WebSocket
transport) to the stock installer. This script re-applies the patches to
a freshly updated `OpenFlux/deploy/node-install.sh`, so upstream changes
are picked up and — more importantly — upstream text drift fails loudly
here instead of breaking channel provisioning on a server.

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
        "# Yandex.Docs WebSocket transport, anonymous, PoW auto-solved) on top\n"
        "# of the stock vyandex=/mailru=/cupsonline=. Everything else is\n"
        "# upstream; the updater runs this fork via install_self.",
    ),
    (
        "declare-var",
        'CHANNEL=""; URL=""; MAILRU=""; CUPS=""; KEY=""; PORT=""; AUTOUPDATE=""',
        'CHANNEL=""; YANDEX=""; URL=""; MAILRU=""; CUPS=""; KEY=""; PORT=""; AUTOUPDATE=""',
    ),
    (
        "parse-key",
        "            channel=*) CHANNEL=${line#channel=} ;;\n            url=*) URL=${line#url=} ;;",
        "            channel=*) CHANNEL=${line#channel=} ;;\n"
        "            yandex=*) YANDEX=${line#yandex=} ;;\n"
        "            url=*) URL=${line#url=} ;;",
    ),
    (
        "validate-key",
        '    [ -z "$URL" ] || valid_url "$URL" || fail input "адрес документа должен быть вида https://docs.yandex.ru/edit/d/..."',
        '    [ -z "$YANDEX" ] || valid_url "$YANDEX" || fail input "адрес документа должен быть вида https://docs.yandex.ru/edit/d/..."\n'
        '    [ -z "$URL" ] || valid_url "$URL" || fail input "адрес документа должен быть вида https://docs.yandex.ru/edit/d/..."',
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
]

PROBES = (
    "FORK (openflux_vpn)",
    "yandex=*) YANDEX=",
    '[ -z "$YANDEX" ] || valid_url',
    "Яндекс Документ (WS)",
    "[Transport yandex]",
)


def build_fork(upstream_text: str) -> str:
    text = upstream_text
    for pid, old, new in PATCHES:
        count = text.count(old)
        if count != 1:
            sys.exit(
                f"FORK DRIFT: patch {pid!r} matches {count} times (need 1). "
                "Upstream node-install.sh changed — rework deploy/make_fork.py."
            )
        text = text.replace(old, new, 1)
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="only verify, do not write")
    args = parser.parse_args()

    upstream = UPSTREAM.read_text(encoding="utf-8")
    forked = build_fork(upstream)

    for probe in PROBES:
        assert probe in forked, f"post-check failed: {probe!r} missing"

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
