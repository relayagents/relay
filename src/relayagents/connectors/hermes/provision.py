"""``UserAgent`` reference: one Hermes Agent container per teammate.

Provisioning is split so that credentials never sit on the shared data volume or inside relay-api:

* ``relay add-user`` (inside relay-api) creates the user, tokens, and AgentCard and prints them.
* ``scripts/add-user.sh`` (on the host) writes the agent's env file under ``var/agents/`` and starts
  ``relay-hermes-<user>`` from ``ghcr.io/relayagents/relay-hermes`` with a per-user named volume.

This class is the programmatic form of the host-side step, used when the CLI runs on a machine
that has Docker (a lab box running Relay outside compose). Without Docker it writes nothing and
returns the command to run. The container satisfies docs/agent-contract.md through
``relay_bridge.py`` (in the image).
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
from pathlib import Path
from typing import Any

from relayagents.core.protocols import A2ATask


class HermesUserAgent:
    name = "hermes"

    def __init__(
        self,
        agents_dir: Path,
        *,
        image: str = "ghcr.io/relayagents/relay-hermes:latest",
        network: str = "relay_default",
        docker: bool = True,
    ) -> None:
        self.agents_dir = Path(agents_dir)
        self.image = image
        self.network = network
        self.docker = docker and shutil.which("docker") is not None

    def env_file_for(self, user_id: str) -> Path:
        return self.agents_dir / f"{user_id}.env"

    def run_command(self, user_id: str) -> list[str]:
        return [
            "docker", "run", "-d", "--name", f"relay-hermes-{user_id}", "--restart", "unless-stopped",
            "--network", self.network, "--env-file", str(self.env_file_for(user_id)),
            "--mount", f"type=volume,source=relay_agent_{user_id},target=/home/hermes", self.image,
        ]  # fmt: skip

    async def provision(self, user_id: str, *, relay_url: str, relay_token: str) -> dict[str, Any]:
        """Write the env file (0600) and start the container. Without Docker, write nothing: the
        token must not be left on a machine that will not run the agent (e.g. inside relay-api)."""
        result: dict[str, Any] = {
            "env_file": None,
            "container": f"relay-hermes-{user_id}",
            "started": False,
            "command": shlex.join(self.run_command(user_id)),
        }
        if not self.docker:
            result["hint"] = (
                "docker is not available here; run scripts/add-user.sh on the node "
                "(it writes the env file and runs the command above)"
            )
            return result
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.agents_dir.chmod(0o700)
        env_file = self.env_file_for(user_id)
        env_file.write_text(
            f"RELAY_URL={relay_url}\nRELAY_TOKEN={relay_token}\nRELAY_USER={user_id}\nRELAY_AGENT_ID={user_id}.hermes\n"
        )
        env_file.chmod(0o600)
        result["env_file"] = str(env_file)
        await self._sh("docker", "rm", "-f", result["container"], check=False)
        rc, out = await self._sh(*self.run_command(user_id))
        result["started"] = rc == 0
        result["output"] = out.strip()[-500:]
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
