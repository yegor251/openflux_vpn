"""Anonymous cups.online room creation for channel provisioning.

The cups service mints a fresh interview room for every anonymous GET of
the live-coding page; the room UUID rides in the HTML (data-room attr).
The packed list is the wire-v2 envelope the deployed core emits and
unpacks: base64url of {"v": 2, "rooms": [uuid, ...]} (no padding).
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.request

ROOM_URL = "https://interview.cups.online/live-coding/"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)

_RE_ROOM = re.compile(r'data-room="\{&quot;uuid&quot;:\s*&quot;([0-9a-f-]{36})&quot;')


class CupsRoomError(RuntimeError):
    """Room creation failed (service refused, wall, or no room in HTML)."""


def create_room(timeout: int = 30) -> str:
    req = urllib.request.Request(
        ROOM_URL,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise CupsRoomError(f"GET room status {resp.status}")
            html = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise CupsRoomError(f"GET room HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CupsRoomError(f"GET room: {exc.reason}") from exc
    m = _RE_ROOM.search(html)
    if not m:
        raise CupsRoomError("комната не создана: uuid в HTML не найден")
    return m.group(1)


def create_rooms(n: int = 8, pause: float = 0.3) -> list[str]:
    ids: list[str] = []
    for i in range(n):
        ids.append(create_room())
        if i < n - 1:
            time.sleep(pause)
    return ids


def pack_rooms(ids: list[str], version: int = 2) -> str:
    payload: dict | list = {"v": version, "rooms": list(ids)} if version >= 2 else list(ids)
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")
