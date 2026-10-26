"""Automatic Yandex Telemost conference creation.

The telemost front API (cloud-api.yandex.ru/telemost_front) accepts a
logged-in Yandex session via cookies and returns a fresh join link. The
request is sent as a captured raw template (urllib's header serialization
gets TelemostBadRequestError; the exact captured bytes do not) with two
runtime substitutions: the Cookie line (from the session file) and a
fresh idempotency-key.

Session file: Netscape cookies.txt of a telemost.yandex.ru session —
must come from a LOGIN NO OLDER THAN THE LAST ONE (telemost rejects
stale sessions with TelemostBadRequestError while passport still accepts
them). Renewal: log into telemost.yandex.ru in a browser and capture one
creation request (see docs/openflux_innersourse/transports/TR-11), or
re-export fresh cookies and re-run the template test.
"""

from __future__ import annotations

import base64
import re
import socket
import ssl
import uuid

HOST = "cloud-api.yandex.ru"

# POST /telemost_front/v2/telemost/conferences?next_gen_media_platform_allowed=true
# captured from the web app (2026-10-26); Cookie and idempotency-key are
# substituted at send time.
_TEMPLATE_B64 = (
    "UE9TVCAvdGVsZW1vc3RfZnJvbnQvdjIvdGVsZW1vc3QvY29uZmVyZW5jZXM/bmV4dF9nZW5fbWVk"
    "aWFfcGxhdGZvcm1fYWxsb3dlZD10cnVlIEhUVFAvMS4xDQpIb3N0OiBjbG91ZC1hcGkueWFuZGV4"
    "LnJ1DQpDb25uZWN0aW9uOiBrZWVwLWFsaXZlDQpDb250ZW50LUxlbmd0aDogMg0Kc2VjLWNoLXVh"
    "LXBsYXRmb3JtOiAibWFjT1MiDQppZGVtcG90ZW5jeS1rZXk6IDY5YTM1MDliLTYzNWMtNDA4OS04"
    "MjFhLWIyMWU5MTkwZWM0YQ0Kc2VjLWNoLXVhOiAiR29vZ2xlIENocm9tZSI7dj0iMTU1IiwgIkNo"
    "cm9taXVtIjt2PSIxNTUiLCAiTm90KEE6QnJhbmQiO3Y9IjI0Ig0Kc2VjLWNoLXVhLW1vYmlsZTog"
    "PzANClgtVGVsZW1vc3QtQ2xpZW50LVZlcnNpb246IDMuMC44DQpDbGllbnQtSW5zdGFuY2UtSWQ6"
    "IDI2MjFkNjkwLTNlODctNDk3Mi1hYWEwLTZmYjYyZGU4MjgxZg0KWC1VaWQ6IDIxMzYwMzk0MTcN"
    "ClVzZXItQWdlbnQ6IE1vemlsbGEvNS4wIChNYWNpbnRvc2g7IEludGVsIE1hYyBPUyBYIDEwXzE1"
    "XzcpIEFwcGxlV2ViS2l0LzUzNy4zNiAoS0hUTUwsIGxpa2UgR2Vja28pIENocm9tZS8xNTUuMC4w"
    "LjAgU2FmYXJpLzUzNy4zNg0KY29udGVudC10eXBlOiBhcHBsaWNhdGlvbi9qc29uDQpBY2NlcHQ6"
    "ICovKg0KT3JpZ2luOiBodHRwczovL3RlbGVtb3N0LnlhbmRleC5ydQ0KU2VjLUZldGNoLVNpdGU6"
    "IHNhbWUtc2l0ZQ0KU2VjLUZldGNoLU1vZGU6IGNvcnMNClNlYy1GZXRjaC1EZXN0OiBlbXB0eQ0K"
    "UmVmZXJlcjogaHR0cHM6Ly90ZWxlbW9zdC55YW5kZXgucnUvDQpBY2NlcHQtRW5jb2Rpbmc6IGd6"
    "aXAsIGRlZmxhdGUsIGJyLCB6c3RkDQpBY2NlcHQtTGFuZ3VhZ2U6IHJ1LVJVLHJ1O3E9MC45LGVu"
    "LVVTO3E9MC44LGVuO3E9MC43DQpDb29raWU6IHBpPTNtVzhVOFZZTmljRlJWZGlFdHFSQVZ0R0Mx"
    "WXFHR3pJM3BleUxwZ2RhOTh4a1lIUUZlMHVDSzdFWkJHbHFIOHJTdFlCeUpkTzBXWUZFdGdDSVo4"
    "MnphY2ZkTzg9OyBpPXZlNndzZW5HaTQreXhiNlhtaVFHKzZHLzhVaUVTd0g3RUQ4OEU4Skh4Z2NS"
    "RmpmWS9MVVE2bE1reXZYQ2FQQnRvUVFzVWZjVklQN0VyeERRN0VMUFFPRWNBYkU9OyB5YW5kZXh1"
    "aWQ9NzU0MjczMzk0MTc5MzAzODU0NjsgeWFzaHI9OTcyNDg1OTI3MTc5MzAzODU0NjsgeXVpZHNz"
    "PTc1NDI3MzM5NDE3OTMwMzg1NDY7IHltZXg9MjEwODM5ODU0Ni55cnRzLjE3OTMwMzg1NDY7IHJl"
    "Y2VpdmUtY29va2llLWRlcHJlY2F0aW9uPTE7IGlzX2dkcHI9MDsgaXNfZ2Rwcl9iPUNLdWpIQkRs"
    "b3dNPTsgZ2Rwcj0wOyBfeW1fdWlkPTE3OTMwMzg1NDkxMTEzMTY1MTk7IF95bV9kPTE3OTMwMzg1"
    "NTA7IGJoPUVrRWlSMjl2WjJ4bElFTm9jbTl0WlNJN2RqMGlNVFUxSWl3Z0lrTm9jbTl0YVhWdElq"
    "dDJQU0l4TlRVaUxDQWlUbTkwS0VFNlFuSmhibVFpTzNZOUlqSTBJaG9EWVhKdElnMHhOVFV1TUM0"
    "NE1EVTVMamcyS2dJL01Eb0hJbTFoWTA5VElrSUdNVFV1Tnk0elNnSTJORkpaSWtkdmIyZHNaU0JE"
    "YUhKdmJXVWlPM1k5SWpFMU5TNHdMamd3TlRrdU9EWWlMQ0pEYUhKdmJXbDFiU0k3ZGowaU1UVTFM"
    "akF1T0RBMU9TNDROaUlzSWs1dmRDaEJPa0p5WVc1a0lqdDJQU0l5TkM0d0xqQXVNQ0pnMWJIKzFn"
    "WnFJZHpLMGJZQnUvR2Zxd1Q2MW9iTUNOTFI3ZXNEL0xtdi93ZmYvWWZWQmZPQkFnPT07IG1kZD0w"
    "OyBTZXNzaW9uX2lkPTM6MTc5MzAzODYwNy41LjAuMTc5MzAzODYwNzY0OTpUal9XSlE6MTQ1Zi4x"
    "LjI6MXwyMTM2MDM5NDE3Li0xLjIuMzoxNzkzMDM4NjA3fDM6MTIyODM4NTQuODk0OTI0LkRPbDVz"
    "ejNDcjNSRnczZWNaSVI2NjFFR1NlUTsgc2Vzc2FyPTEuMjE5MDExMS5DaUQxazNpLTlsaV9YUFh4"
    "eEVObE4yUWxpaVhIYktqR21IaEsydnZ3N0dmaVFRLnM2N0VraGNJckRaM2ZNa3ZyanBOR1RVT254"
    "Zmtkc2lRNmVjRHJHd1VYUDQ7IHNlc3Npb25pZDI9MzoxNzkzMDM4NjA3LjUuMC4xNzkzMDM4NjA3"
    "NjQ5OlRqX1dKUToxNDVmLjEuMjoxfDIxMzYwMzk0MTcuLTEuMi4zOjE3OTMwMzg2MDd8MzoxMjI4"
    "Mzg1NC44OTQ5MjQuZmFrZXNpZ24wMDAwMDAwMDAwMDAwMDAwMDAwOyB5cD0yMTA4Mzk4NjA3LnVk"
    "bi5jRHBsWjI5eWIzWnZiR1ZuYjNacGRHTm87IEw9VlZsZ1YxWkdRMTl4WEE5aFVGTmhVbnhOREhV"
    "Qlp3WmlKeFFCQlFvWkdCNG1WRjhCWFNZSkh3PT0uMTc5MzAzODYwNy4yMjc0ODI5LjM4OTEwMC5i"
    "NmNkZDU0ZjVkOTUxMmNkMGJlYTBiNjRjOWU4ZGQ4MTsgeWFuZGV4X2xvZ2luPWVnb3Jvdm9sZWdv"
    "dml0Y2g7IF95YXNjPWFFS05Jc0FxdWc3cmI5SjRKb3FvcGZ1VWRadUdmYUMwNWZRWnZ6ZmRlT3pv"
    "SEswRDExaFpmQlRkK29wRzFGcmNYRDlkdHZaRE5GQmgwK0c4cG9WaXF5bjdHVHl2VmtkemdrR3JZ"
    "SXpyTEppd1pZcjM7IHlzPXVkbi5jRHBsWjI5eWIzWnZiR1ZuYjNacGRHTm8jY19jaGNrLjM3NTMz"
    "MjI2MDcNCg0Ke30="
)


class TelemostAuthError(RuntimeError):
    """The session cookies were rejected (stale session — renew it)."""


def _cookie_line(session_file: str) -> str:
    jar = {}
    for line in open(session_file, encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 7:
            jar[parts[5]] = parts[6]
    if not jar:
        raise TelemostAuthError("файл сессии telemost пуст")
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _request_bytes(cookie_line: str) -> bytes:
    raw = base64.b64decode(_TEMPLATE_B64)
    raw = re.sub(rb"(?m)^Cookie: [^\r]*", ("Cookie: " + cookie_line).encode(), raw)
    raw = re.sub(
        rb"(?m)^idempotency-key: [^\r]*",
        ("idempotency-key: " + str(uuid.uuid4())).encode(),
        raw,
    )
    return raw


def create_conference(session_file: str, timeout: float = 20.0) -> str:
    """Create a conference, return its join URL (https://telemost.yandex.ru/j/<id>)."""
    req = _request_bytes(_cookie_line(session_file))
    resp = b""
    with socket.create_connection((HOST, 443), timeout=timeout) as sock:
        with ssl.create_default_context().wrap_socket(sock, server_hostname=HOST) as tls:
            tls.sendall(req + b"\r\n")
            tls.settimeout(timeout)
            try:
                while len(resp) < 262144:
                    chunk = tls.read(16384)
                    if not chunk:
                        break
                    resp += chunk
            except OSError:
                pass
    status = (resp.partition(b"\r\n\r\n")[0].split(b"\r\n")[0]).decode(errors="replace")
    m = re.search(rb'"uri"\s*:\s*"(https://telemost\.yandex\.ru/j/[^"]+)"', resp)
    if m:
        return m.group(1).decode()
    if "401" in status or "Unauthorized" in status:
        raise TelemostAuthError(f"сессия telemost отклонена ({status}) — обнови файл сессии")
    raise RuntimeError(f"создание конференции не удалось: {status}")

