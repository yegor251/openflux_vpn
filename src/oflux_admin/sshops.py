"""Thin paramiko wrapper: run commands and upload files over SSH."""

from __future__ import annotations

import shlex
from pathlib import Path

import paramiko


class SSHOps:
    def __init__(self, host: str, user: str = "root", key_path: str = "", timeout: float = 30.0) -> None:
        self.host = host
        self.client = paramiko.SSHClient()
        # TOFU, like the core's provisioner: remember what we saw first
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict = {"hostname": host, "username": user, "timeout": timeout}
        if key_path:
            kwargs["key_filename"] = str(Path(key_path).expanduser())
        else:
            kwargs["key_filename"] = str(Path("~/.ssh/id_ed25519").expanduser())
        self.client.connect(**kwargs)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "SSHOps":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def run(self, command: str, input_data: str | bytes | None = None, timeout: float = 600.0) -> tuple[int, str, str]:
        """Run a command; returns (exit_code, stdout, stderr)."""
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        if input_data is not None:
            if isinstance(input_data, str):
                input_data = input_data.encode()
            stdin.write(input_data)
            stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err

    def put_file(self, local: str | Path, remote: str, mode: int = 0o600) -> None:
        sftp = self.client.open_sftp()
        try:
            sftp.put(str(local), remote)
            sftp.chmod(remote, mode)
        finally:
            sftp.close()

    def put_text(self, text: str, remote: str, mode: int = 0o600) -> None:
        sftp = self.client.open_sftp()
        try:
            with sftp.open(remote, "w") as f:
                f.write(text)
            sftp.chmod(remote, mode)
        finally:
            sftp.close()

    def quote(self, arg: str) -> str:
        return shlex.quote(arg)
