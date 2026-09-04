"""``CodingAgent`` implementations: headless CLI invocations in the sandbox container.

Each agent is a command template. The sandbox has the CLIs installed and reaches Relay's MCP
server with the *user's* agent token (``RELAY_TOKEN``), so `relay my_items` / `relay report`
work from inside the run. Nothing here talks to a model provider: the coding agent does, with
the key the user configured for it.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from relayagents.core.protocols import CodingRun


@dataclass(frozen=True)
class CliCodingAgent:
    name: str
    argv_template: tuple[str, ...]  # "{prompt}" is replaced
    runner: str = "local"  # local | docker
    image: str = ""
    docker_network: str = "relay_default"

    def argv(self, prompt: str) -> list[str]:
        return [a.replace("{prompt}", prompt) for a in self.argv_template]

    async def run(
        self, prompt: str, *, workdir: Path, env: dict[str, str], timeout_s: int = 1800
    ) -> CodingRun:
        argv = self.argv(prompt)
        if self.runner == "docker":
            docker_env = [x for k, v in env.items() for x in ("-e", f"{k}={v}")]
            argv = [
                "docker",
                "run",
                "--rm",
                "--network",
                self.docker_network,
                "-v",
                f"{workdir}:/work",
                "-w",
                "/work",
                *docker_env,
                self.image,
                *argv,
            ]
            env = {}
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=None if self.runner == "docker" else workdir,
            env={**env} if env else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            return CodingRun(
                agent=self.name,
                exit_code=-1,
                stdout_tail="",
                stderr_tail=f"timeout after {timeout_s}s: {shlex.join(argv[:3])}",
                duration_s=time.perf_counter() - t0,
            )
        return CodingRun(
            agent=self.name,
            exit_code=proc.returncode or 0,
            stdout_tail=out.decode(errors="replace")[-4000:],
            stderr_tail=err.decode(errors="replace")[-2000:],
            duration_s=time.perf_counter() - t0,
        )


AGENTS: dict[str, tuple[str, ...]] = {
    "claude-code": (
        "claude",
        "-p",
        "{prompt}",
        "--output-format",
        "text",
        "--permission-mode",
        "acceptEdits",
    ),
    "codex": ("codex", "exec", "--full-auto", "{prompt}"),
    "opencode": ("opencode", "run", "{prompt}"),
}


def get_coding_agent(name: str, *, runner: str = "local", image: str = "") -> CliCodingAgent:
    try:
        return CliCodingAgent(name=name, argv_template=AGENTS[name], runner=runner, image=image)
    except KeyError as exc:
        raise KeyError(f"unknown coding agent {name!r}; known: {', '.join(AGENTS)}") from exc


def sandbox_prompt(issue_url: str, item_id: str, summary: str) -> str:
    return (
        f"You are working on {issue_url} for Relay action item {item_id}: {summary}\n"
        "Relay is available as an MCP server named 'relay'. Start by calling relay my_items to see context, "
        "call relay report with short progress notes as you go, and finish by calling relay report with "
        f"item_id={item_id} describing what you changed and linking the branch or PR. Work on a branch; do not push to main."
    )
