"""Drive node-install.sh (our fork) over SSH to manage exit channels."""

from __future__ import annotations

import json
from pathlib import Path

from .sshops import SSHOps

CONF_ROOT = "/etc/openflux-node"


def build_channel_config(
    *,
    channel: str,
    key: str,
    port: int,
    yandex_url: str = "",
    mailru_url: str = "",
    mode: str = "",
    autoupdate: str = "yes",
) -> str:
    """The key=value config node-install.sh apply reads (stdin or file)."""
    lines = [f"channel={channel}"]
    if yandex_url:
        lines.append(f"yandex={yandex_url}")
    if mailru_url:
        lines.append(f"mailru={mailru_url}")
    if mode:
        lines.append(f"mode={mode}")
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
    REMOTE_WARMUP_KEY = "/tmp/oflux-warmup-{channel}.key"
    BIN = "/opt/openflux-node/bin/openflux"

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
        result = self._run_script(f"apply {self.ops.quote(remote_conf)}")
        result["warmed"] = self.warmup(channel, config)
        return result

    def warmup(self, channel: str, config: str) -> bool:
        """Consume the first-connection race on a freshly started node.

        The first client connection after a node start sometimes lands in
        a zombie session: the handshake completes, but the data plane
        never relays (upstream lane binding race, seen on l3 and l4).
        The next connection to the same node is always fine, so right
        after apply we open a throwaway loopback session to the channel
        port and let the user's connection be the second one.
        """
        key = port = ""
        for line in config.strip().splitlines():
            if line.startswith("key="):
                key = line[4:]
            elif line.startswith("port="):
                port = line[5:]
        if not key or not port:
            return False
        remote_conf = f"/tmp/oflux-warmup-{channel}.conf"
        conf = (
            "Role = client\n"
            "Inbound = socks5\n"
            f"Socks5 = 127.0.0.1:18299\n"
            f"EncryptionKeyFile = {self.REMOTE_WARMUP_KEY.format(channel=channel)}\n"
            "\n"
            "[Transport direct]\n"
            "Type = direct\n"
            "Priority = 50\n"
            f"Dial = 127.0.0.1:{port}\n"
        )
        try:
            self.ops.put_text(f"{key}\n", self.REMOTE_WARMUP_KEY.format(channel=channel), mode=0o600)
            self.ops.put_text(conf, remote_conf, mode=0o600)
            self.ops.run(
                f"timeout 6 {self.BIN} --config {self.ops.quote(remote_conf)} >/dev/null 2>&1;"
                f" rc=$?; rm -f {self.ops.quote(remote_conf)} {self.ops.quote(self.REMOTE_WARMUP_KEY.format(channel=channel))};"
                " exit 0"
            )
            return True
        except Exception:  # noqa: BLE001 - warm-up is best-effort
            return False

    def remove(self, channel: str) -> dict:
        return self._run_script(f"remove {self.ops.quote(channel)}")

    def list(self) -> dict:
        return self._run_script("list")

    def channel_mode(self, channel: str) -> str:
        """The channel's exit mode from its node.conf ('' if unknown)."""
        rc, out, _ = self.ops.run(
            f"grep -h '^Mode' {CONF_ROOT}/{self.ops.quote(channel)}/node.conf 2>/dev/null || true"
        )
        line = out.strip()
        return line.split("=", 1)[1].strip() if "=" in line else ""
