"""Shared test doubles: a scriptable fake requests.Session."""

from __future__ import annotations


class FakeCookies:
    def __init__(self) -> None:
        self.store: dict = {}

    def set(self, name, value, domain=None, path=None, secure=None):
        self.store[name] = (value, domain, path, secure)


class FakeResponse:
    def __init__(self, status_code=200, text="", url="", history=None, payload=None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.history = history or []
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        import json

        return json.loads(self.text)


class _Request:
    def __init__(self, url, params=None, data=None, json_body=None, files=None, headers=None):
        self.url = url
        self.params = params
        self.data = data
        self.json_body = json_body
        self.files = files
        self.headers = headers


class FakeSession:
    """Routes calls to handlers matched by (method, url substring).

    Each handler is ``fn(request) -> FakeResponse`` where request carries
    url, params, data, json_body, files. First match wins; a missing match
    fails the test loudly.
    """

    def __init__(self) -> None:
        self.cookies = FakeCookies()
        self.headers: dict = {}
        self.calls: list = []
        self.handlers: list = []

    def on(self, method: str, url_part: str, fn) -> None:
        self.handlers.append((method, url_part, fn))

    def _find(self, method: str, url: str):
        for m, part, fn in self.handlers:
            if m == method and part in url:
                return fn
        raise AssertionError(f"unexpected {method} {url}")

    def _record(self, method, url, **kw):
        request = _Request(url, **kw)
        self.calls.append((method, url, request))
        return self._find(method, url)(request)

    def get(self, url, timeout=None, allow_redirects=True, params=None, **kw):
        return self._record("GET", url, params=params, **kw)

    def post(self, url, data=None, json=None, files=None, headers=None,
             timeout=None, params=None, **kw):
        return self._record("POST", url, data=data, json_body=json,
                            files=files, headers=headers, params=params)

    def put(self, url, data=None, headers=None, timeout=None, params=None, **kw):
        return self._record("PUT", url, data=data, headers=headers, params=params)
