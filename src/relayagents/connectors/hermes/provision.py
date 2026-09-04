"""``UserAgent`` reference: one Hermes Agent container per teammate.

Provisioning writes a per-user directory under ``RELAY_DATA_DIR/agents/<user>/`` containing the
agent's env (its Relay token and URL) and the ``relay`` skill, then starts a container from
``ghcr.io/relayagents/relay-hermes`` with that directory mounted as the agent's home.

The container satisfies docs/agent-contract.md through ``relay_bridge.py`` (in the image): it
long-polls the A2A inbox and invokes Hermes headlessly for each task, so it does not depend on
Hermes internals beyond "run a prompt from the CLI".
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from relayagents.core.protocols import A2ATask


class HermesUserAgent:
    name = "hermes"

    def __init__(
        self,
        data_dir: Path,
        *,
        image: str = "ghcr.io/relayagents/relay-hermes:latest",
        network: str = "relay_default",
        docker: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.image = image
        self.network = network
        self.docker = docker and shutil.which("docker") is not None

    def home_for(self, user_id: str) -> Path:
        return self.data_dir / "agents" / user_id

    async def provision(self, user_id: str, *, relay_url: str, relay_token: str) -> dict[str, Any]:
        home = self.home_for(user_id)
        home.mkdir(parents=True, exist_ok=True)
        env_file = home / "relay.env"
        env_file.write_text(
            f"RELAY_URL={relay_url}\nRELAY_TOKEN={relay_token}\nRELAY_USER={user_id}\nRELAY_AGENT_ID={user_id}.hermes\n"
        )
        env_file.chmod(0o600)
        (home / "relay.json").write_text(
            json.dumps({"url": relay_url, "token": relay_token, "user_id": user_id}, indent=2)
        )
        (home / "relay.json").chmod(0o600)
        container = f"relay-hermes-{user_id}"
        result: dict[str, Any] = {"home": str(home), "container": container, "started": False}
        if not self.docker:
            result["hint"] = (
                f"docker not available here; start the agent with: docker run -d --name {container} --network {self.network} --env-file {env_file} -v {home}:/home/hermes {self.image}"
            )
            return result
        await self._sh("docker", "rm", "-f", container, check=False)
        rc, out = await self._sh(
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--restart",
            "unless-stopped",
            "--network",
            self.network,
            "--env-file",
            str(env_file),
            "-v",
            f"{home}:/home/hermes",
            self.image,
        )
        result["started"] = rc == 0
        result["output"] = out.strip()
        return result

    async def deliver(self, task: A2ATask) -> bool:
        return False  # Hermes pulls from its inbox; nothing to push.

    async def _sh(self, *argv: str, check: bool = True) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        if check and proc.returncode != 0:
            raise RuntimeError(out.decode())
        return proc.returncode or 0, out.decode()
