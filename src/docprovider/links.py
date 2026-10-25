"""Build and verify openflux:// links via the real openflux binary.

The link codec (deflate+base64url JSON carrying the encryption key and
transports) is not reimplemented here: the binary is needed for deployment
anyway, so we let it produce and parse links. That keeps us immune to
format drift between OpenFlux versions.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from .errors import OpenFluxBinaryError
from .models import ChannelDocs

# priority = failover order; the most alive high-priority transport wins
DIRECT_PRIORITY = 200
YANDEX_PRIORITY = 100
MAILRU_PRIORITY = 50


def find_binary(explicit: str | None = None) -> str:
    binary = explicit or shutil.which("openflux")
    if not binary:
        raise OpenFluxBinaryError(
            "openflux binary not found: pass binary= or set OPENFLUX_BIN / PATH"
        )
    return binary


def _run(binary: str, flag: str, payload: str) -> dict:
    try:
        proc = subprocess.run(
            [binary, flag, "-"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError as exc:
        raise OpenFluxBinaryError(f"cannot run {binary}: {exc}") from exc
    try:
        result = json.loads(proc.stdout)
    except ValueError as exc:
        raise OpenFluxBinaryError(
            f"openflux {flag} printed no JSON (stderr: {proc.stderr.strip()[:120]})"
        ) from exc
    if "error" in result:
        raise OpenFluxBinaryError(
            f"openflux {flag}: {result['error']}"
            + (f" [{result['code']}: {result['param']}]" if result.get("code") else "")
        )
    return result


def make_link(
    *,
    name: str,
    secret: str,
    docs: ChannelDocs | None = None,
    direct_dial: str | None = None,
    cups_url: str | None = None,
    telemost_url: str | None = None,
    binary: str | None = None,
) -> str:
    """Compose the user-facing openflux:// link for one channel.

    The l3/l4 backend is chosen by the exit node, not the link, so it is
    not part of the payload (share.Config mode is only for phpbox stream).
    Carrier-product channels pass cups_url or telemost_url and neither
    docs nor direct_dial: the link then carries the carrier only, so no
    client leg ever touches the VDS address.
    """
    binary = find_binary(binary)
    transports: list[dict] = []
    if cups_url:
        transports.append(
            {"type": "cupsonline", "name": "cupsonline", "url": cups_url, "priority": 100}
        )
    if telemost_url:
        transports.append(
            {"type": "telemost", "name": "telemost", "url": telemost_url, "priority": 100}
        )
    if direct_dial:
        transports.append(
            {"type": "direct", "name": "direct", "dial": direct_dial, "priority": DIRECT_PRIORITY}
        )
    if docs:
        transports.append(
            {"type": "yandex", "name": "yandex", "url": docs.yandex.url, "priority": YANDEX_PRIORITY}
        )
        if docs.mailru:
            transports.append(
                {"type": "mailru", "name": "mailru", "url": docs.mailru.url, "priority": MAILRU_PRIORITY}
            )
    config = {
        "name": name,
        "negotiate": True,
        "codec": "batched",
        "secret": secret,
        "transports": transports,
    }
    result = _run(binary, "--make-link", json.dumps(config))
    return result["link"]


def parse_link(link: str, binary: str | None = None) -> dict:
    """Parse an openflux:// link back into its config (self-check helper)."""
    binary = find_binary(binary)
    result = _run(binary, "--parse-link", link)
    return result.get("config", result)
