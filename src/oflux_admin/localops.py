"""Local command execution with the SSHOps interface.

Lets Provisioner drive node-install.sh on the bot's own VPS without
SSH-ing to localhost. Same surface as SSHOps: run / put_file /
put_text / quote.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


class LocalOps:
    def run(self, cmd: str, input_data: str | None = None, timeout: float = 900.0):
        proc = subprocess.run(
            ["/bin/sh", "-c", cmd],
            input=input_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def put_file(self, local: str | Path, remote: str | Path, mode: int = 0o600) -> None:
        self.put_text_bytes(Path(local).read_bytes(), remote, mode)

    def put_text(self, text: str, remote: str | Path, mode: int = 0o600) -> None:
        self.put_text_bytes(text.encode("utf-8"), remote, mode)

    def put_text_bytes(self, data: bytes, remote: str | Path, mode: int) -> None:
        path = Path(remote)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    @staticmethod
    def quote(value: str) -> str:
        return shlex.quote(value)
