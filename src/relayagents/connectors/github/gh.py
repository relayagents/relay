"""``IssueTracker`` via the ``gh`` CLI, always with the *user's* token (``GH_TOKEN``).

Relay never holds a GitHub token of its own. The user's agent runs this with the token it was
provisioned with (per-user), or the sandbox runs it with the token injected for that run.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Sequence
from typing import Any

from relayagents.core.protocols import IssueRef


class GhError(RuntimeError):
    pass


class GhIssueTracker:
    def __init__(self, token_for_user: Any | None = None, gh_bin: str = "gh") -> None:
        """``token_for_user(user_id) -> str`` supplies the per-user token; defaults to ``GH_TOKEN`` env."""
        self.token_for_user = token_for_user
        self.gh_bin = gh_bin

    async def _run(self, user_id: str, *args: str) -> str:
        if shutil.which(self.gh_bin) is None:
            raise GhError("gh CLI not installed")
        env = dict(os.environ)
        if self.token_for_user is not None:
            env["GH_TOKEN"] = self.token_for_user(user_id)
        if not env.get("GH_TOKEN") and not env.get("GITHUB_TOKEN"):
            raise GhError(
                f"no GitHub token for user {user_id!r} (set GH_TOKEN in the agent's environment)"
            )
        proc = await asyncio.create_subprocess_exec(
            self.gh_bin,
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise GhError(err.decode().strip() or f"gh exited {proc.returncode}")
        return out.decode()

    async def create_issue(
        self, user_id: str, repo: str, title: str, body: str, *, labels: Sequence[str] = ()
    ) -> IssueRef:
        args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
        for lb in labels:
            args += ["--label", lb]
        url = (await self._run(user_id, *args)).strip().splitlines()[-1]
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
        return IssueRef(url=url, number=number, repo=repo)

    async def list_issues(
        self, user_id: str, repo: str, *, assignee: str | None = None, state: str = "open"
    ) -> list[IssueRef]:
        args = [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--json",
            "number,url",
            "--limit",
            "100",
        ]
        if assignee:
            args += ["--assignee", assignee]
        data = json.loads(await self._run(user_id, *args) or "[]")
        return [IssueRef(url=i["url"], number=i["number"], repo=repo) for i in data]

    async def user_activity(
        self, user_id: str, login: str, *, since_iso: str
    ) -> list[dict[str, Any]]:
        """Recent PRs/issues/commits by the user, for standup drafts. Best-effort."""
        q = f"author:{login} updated:>={since_iso[:10]}"
        raw = await self._run(
            user_id, "search", "prs", q, "--json", "title,url,state,updatedAt", "--limit", "20"
        )
        return list(json.loads(raw or "[]"))
