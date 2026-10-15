"""Drive node-install.sh (our fork) over SSH to manage exit channels."""

from __future__ import annotations

import json
from pathlib import Path

from .sshops import SSHOps


def build_channel_config(
    *,
    channel: str,
    key: str,
    port: int,
    yandex_url: str = "",
    mailru_url: str = "",
    autoupdate: str = "yes",
) -> str:
    """The key=value config node-install.sh apply reads (stdin or file)."""
    lines = [f"channel={channel}"]
    if yandex_url:
        lines.append(f"yandex={yandex_url}")
    if mailru_url:
        lines.append(f"mailru={mailru_url}")
    lines.append(f"key={key}")
    lines.append(f"port={port}")
    lines.append(f"autoupdate={autoupdate}")
    return "\n".join(lines) + "\n"


def parse_json_line(output: str) -> dict:
    """node-install.sh prints exactly one JSON object line; skip noise."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError(f"no JSON in installer output: {output[:200]!r}")


class Provisioner:
    """Runs our node-install.sh fork on a server. Root assumed (direct
    login or sudo-wrapped user is a later concern)."""

    REMOTE_SCRIPT = "/tmp/oflux-node-install.sh"
    REMOTE_CONFIG = "/tmp/oflux-channel-{channel}.conf"

    def __init__(self, ops: SSHOps, installer_path: str | Path) -> None:
        self.ops = ops
        self.installer_local = Path(installer_path)

    def _run_script(self, args: str, input_data: str | None = None, timeout: float = 900.0) -> dict:
        rc, out, err = self.ops.run(
            f"sh {self.ops.quote(self.REMOTE_SCRIPT)} {args}".strip(),
            input_data=input_data,
            timeout=timeout,
        )
        if rc != 0:
            raise RuntimeError(f"installer {args!r} failed (rc={rc}): {err.strip() or out.strip()[:300]}")
        return parse_json_line(out)

    def sync_installer(self) -> None:
        self.ops.put_file(self.installer_local, self.REMOTE_SCRIPT, mode=0o755)

    def probe(self) -> dict:
        return self._run_script("probe")

    def plan(self, config: str) -> dict:
        # plan reads the config from stdin and changes nothing
        rc, out, err = self.ops.run(
            f"sh {self.ops.quote(self.REMOTE_SCRIPT)} plan",
            input_data=config,
        )
        if rc != 0:
            raise RuntimeError(f"plan failed: {err.strip() or out.strip()[:300]}")
        return parse_json_line(out)

    def apply(self, channel: str, config: str) -> dict:
        remote_conf = self.REMOTE_CONFIG.format(channel=channel)
        self.ops.put_text(config, remote_conf, mode=0o600)
        # the installer deletes the config file after reading it
        return self._run_script(f"apply {self.ops.quote(remote_conf)}")

    def remove(self, channel: str) -> dict:
        return self._run_script(f"remove {self.ops.quote(channel)}")

    def list(self) -> dict:
        return self._run_script("list")
