"""`relay`: login, setup-agent, add-user, meeting upload, replay, standup, serve/worker, + the tool surface."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from relayagents import __version__
from relayagents.cli.client import CONFIG_DIR, Credentials, RelayClient
from relayagents.tools.cli import register_tool_commands

app = typer.Typer(
    help="Relay: your team's shared memory and agent switchboard.",
    no_args_is_help=True,
    rich_markup_mode="markdown",
)
meeting_app = typer.Typer(help="Meetings: upload audio or transcripts.")
standup_app = typer.Typer(help="Standups on behalf of a teammate (used by the agent's cron).")
app.add_typer(meeting_app, name="meeting")
app.add_typer(standup_app, name="standup")


def _client() -> RelayClient:
    return RelayClient()


def _echo_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


@app.callback()
def _root(version: Annotated[bool, typer.Option("--version", is_eager=True)] = False) -> None:
    if version:
        typer.echo(f"relay {__version__}")
        raise typer.Exit()


# ---- login / whoami ----------------------------------------------------------------------------


@app.command()
def login(
    url: Annotated[str, typer.Option(help="Relay URL, e.g. https://relay.relayagents.dev")] = "",
    user: Annotated[str, typer.Option(help="Your Relay user id")] = "",
    token: Annotated[str, typer.Option(help="Paste a token instead of the Slack device flow")] = "",
) -> None:
    """Obtain a per-user token. Default: device flow approved from your Slack DM."""
    url = (url or os.environ.get("RELAY_URL") or typer.prompt("Relay URL")).rstrip("/")
    if token:
        creds = Credentials(url=url, token=token, user_id=user)
        try:
            me = RelayClient(creds).whoami()
        except Exception as exc:
            typer.secho(f"token rejected: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc
        creds.user_id = me["user"]["id"]
        creds.save()
        typer.echo(
            f"logged in as {creds.user_id} ({me['actor']['id']}); credentials in {CONFIG_DIR}"
        )
        return
    user = user or typer.prompt("Relay user id")
    import httpx

    r = httpx.post(
        f"{url}/v1/auth/device",
        json={"user_id": user, "label": f"cli@{os.uname().nodename}"},
        timeout=30,
    )
    if r.status_code >= 400:
        typer.secho(f"error: {r.text}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    d = r.json()
    if d.get("slack"):
        typer.echo(
            f"Check your Slack DM from Relay and approve login code {d['user_code']} (expires in 10 min)."
        )
    else:
        typer.echo(
            f"Slack is not configured. Ask an admin to run on the node:\n  relay admin approve-login {d['device_code']}\n(code {d['user_code']})"
        )
    deadline = time.time() + d.get("expires_in", 600)
    while time.time() < deadline:
        time.sleep(d.get("interval", 3))
        p = httpx.get(f"{url}/v1/auth/device/{d['device_code']}", timeout=30).json()
        if p["status"] == "approved":
            Credentials(url=url, token=p["token"], user_id=user).save()
            typer.echo(f"logged in as {user}; credentials in {CONFIG_DIR}")
            return
        if p["status"] in ("denied", "expired"):
            typer.secho(f"login {p['status']}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
    typer.secho("login timed out", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@app.command()
def whoami() -> None:
    """Show the current user, actor, and registered agents."""
    _echo_json(_client().whoami())


@app.command()
def me(
    standup_mode: Annotated[str | None, typer.Option(help="draft | auto | off")] = None,
    standup_time: Annotated[str | None, typer.Option(help="HH:MM local")] = None,
    timezone: Annotated[str | None, typer.Option()] = None,
    slack_user_id: Annotated[str | None, typer.Option(help="Your Slack member id (U...)")] = None,
    github_login: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Update your Relay settings (posting mode, timezone, Slack/GitHub identities)."""
    body = {
        k: v
        for k, v in {
            "standup_mode": standup_mode,
            "standup_time": standup_time,
            "timezone": timezone,
            "slack_user_id": slack_user_id,
            "github_login": github_login,
        }.items()
        if v is not None
    }
    if not body:
        _echo_json(_client().whoami()["user"])
        return
    c = _client()
    r = c.http.patch("/v1/me", json=body)
    c._raise(r)
    _echo_json(r.json())


# ---- setup-agent -------------------------------------------------------------------------------

MCP_SNIPPETS = {
    "claude-code": lambda url, token: {
        "cmd": f'claude mcp add --transport http relay "{url}/mcp" --header "Authorization: Bearer {token}"',
        "file": None,
        "content": None,
    },
    "codex": lambda url, token: {
        "cmd": None,
        "file": "~/.codex/config.toml",
        "content": f'[mcp_servers.relay]\nurl = "{url}/mcp"\nbearer_token_env_var = "RELAY_TOKEN"\n',
    },
    "opencode": lambda url, token: {
        "cmd": None,
        "file": "opencode.json",
        "content": json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {
                    "relay": {
                        "type": "remote",
                        "url": f"{url}/mcp",
                        "enabled": True,
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                },
            },
            indent=2,
        ),
    },
    "hermes": lambda url, token: {
        "cmd": None,
        "file": "~/.hermes/config.yaml",
        "content": f"mcp_servers:\n  relay:\n    url: {url}/mcp\n    headers:\n      Authorization: Bearer {token}\n",
    },
    "generic": lambda url, token: {
        "cmd": None,
        "file": None,
        "content": json.dumps(
            {
                "mcpServers": {
                    "relay": {
                        "type": "http",
                        "url": f"{url}/mcp",
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                }
            },
            indent=2,
        ),
    },
}


@app.command("setup-agent")
def setup_agent(
    agent: Annotated[str, typer.Argument(help="claude-code | codex | opencode | hermes | generic")],
    write: Annotated[
        bool, typer.Option(help="Write the config file / run the command instead of printing.")
    ] = False,
    agent_token: Annotated[
        bool,
        typer.Option(
            help="Mint a dedicated agent token (actor <you>.<agent>) instead of reusing your human token."
        ),
    ] = True,
) -> None:
    """Point a coding agent or user agent at Relay's MCP server."""
    if agent not in MCP_SNIPPETS:
        typer.secho(
            f"unknown agent {agent!r}; choose from {', '.join(MCP_SNIPPETS)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    c = _client()
    token = c.creds.token
    if agent_token and agent != "generic":
        token = c.post(
            "/v1/tokens", {"label": f"agent:{agent}", "actor_kind": "agent", "harness": agent}
        )["token"]
    snippet = MCP_SNIPPETS[agent](c.creds.url, token)
    if snippet["cmd"]:
        typer.echo(snippet["cmd"] if not write else "running: " + snippet["cmd"])
        if write:
            os.system(snippet["cmd"])
        return
    typer.echo(f"# {snippet['file'] or 'MCP config'}\n{snippet['content']}")
    if write and snippet["file"]:
        path = Path(os.path.expanduser(snippet["file"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write("\n" + snippet["content"])
        typer.echo(f"appended to {path}")
    if agent == "codex":
        typer.echo(f"\nexport RELAY_TOKEN={token}")


# ---- add-user (runs on the Relay node, talks to the DB directly) -------------------------------


@app.command("add-user")
def add_user(
    user_id: Annotated[str, typer.Argument(help="short id, e.g. ada")],
    name: Annotated[str, typer.Option(help="Display name")] = "",
    email: Annotated[str | None, typer.Option()] = None,
    slack_user_id: Annotated[str | None, typer.Option()] = None,
    github_login: Annotated[str | None, typer.Option()] = None,
    timezone: Annotated[str, typer.Option()] = "UTC",
    admin: Annotated[bool, typer.Option("--admin", help="Make this user an admin")] = False,
    no_container: Annotated[
        bool, typer.Option("--no-container", help="Register only; do not start a Hermes container")
    ] = False,
    reissue: Annotated[
        bool, typer.Option("--reissue", help="Mint new tokens for an existing user")
    ] = False,
    agents_dir: Annotated[
        Path, typer.Option(help="Where per-user agent env files are written (host side)")
    ] = Path("var/agents"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON (used by scripts/add-user.sh)"),
    ] = False,
) -> None:
    """Provision a teammate: user, tokens, AgentCard, and (with Docker on this machine) a Hermes container.

    Inside the relay-api container there is no Docker; run scripts/add-user.sh on the node instead.
    """
    from fastapi import HTTPException

    from relayagents.api.app import build_services
    from relayagents.api.routes.users import AddUserIn, create_user_with_tokens
    from relayagents.connectors.hermes import HermesUserAgent
    from relayagents.core.config import get_settings
    from relayagents.core.events import Actor

    settings = get_settings()

    async def run() -> None:
        services = build_services(settings)
        if settings.is_sqlite:
            await services.db.create_all()
        try:
            out = await create_user_with_tokens(
                services,
                AddUserIn(
                    id=user_id,
                    display_name=name or user_id,
                    email=email,
                    slack_user_id=slack_user_id,
                    github_login=github_login,
                    timezone=timezone,
                    is_admin=admin,
                    reissue=reissue,
                ),
                issued_by=Actor.system("relay.cli.admin"),
            )
        except HTTPException as exc:
            typer.secho(f"error: {exc.detail}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc
        finally:
            await services.db.dispose()
        prov: dict[str, Any] = {"started": False}
        if not no_container:
            prov = await HermesUserAgent(agents_dir).provision(
                user_id, relay_url=settings.public_url, relay_token=out.agent_token
            )
        if json_output:
            _echo_json({**out.model_dump(), "provision": prov})
            return
        typer.echo(
            f"user {out.user.id} {'updated' if reissue else 'created'}. Agent {out.agent_id} registered."
        )
        if no_container:
            typer.echo(
                "hermes: not started (--no-container). On the node: scripts/add-user.sh " + user_id
            )
        elif prov["started"]:
            typer.echo(f"hermes: started {prov['container']} (credentials in {prov['env_file']})")
        else:
            typer.echo(f"hermes: {prov.get('hint')}\n  {prov.get('command')}")
        typer.echo(
            "\n== give this to the person (human token; used once by `relay login --token`) =="
        )
        typer.echo(out.human_token)
        typer.echo(
            "\n== MCP config for their coding agent (or run `relay setup-agent <agent>` after login) =="
        )
        typer.echo(MCP_SNIPPETS["generic"](out.relay_url, out.human_token)["content"])

    asyncio.run(run())


admin_app = typer.Typer(help="Node-side admin commands.")
app.add_typer(admin_app, name="admin")


@admin_app.command("approve-login")
def approve_login(device_code: str) -> None:
    """Approve a `relay login` request when Slack is not configured (run on the node)."""
    from relayagents.api.app import build_services
    from relayagents.api.routes.users import approve_device
    from relayagents.core.config import get_settings

    async def run() -> None:
        from relayagents.core.events import Actor

        services = build_services(get_settings())
        row = await approve_device(
            services, device_code, approved=True, admin=Actor.system("relay.cli.admin")
        )
        await services.db.dispose()
        typer.echo(f"login {row.user_code}: {row.status}")

    asyncio.run(run())


# ---- meetings ----------------------------------------------------------------------------------


@meeting_app.command("upload")
def meeting_upload(
    path: Annotated[Path | None, typer.Argument(help="Audio file (m4a/mp3/wav/...)")] = None,
    transcript: Annotated[Path | None, typer.Option(help="Transcript JSON (see fixtures/)")] = None,
    skip_asr: Annotated[
        bool, typer.Option("--skip-asr", help="Use --transcript instead of running ASR")
    ] = False,
    title: Annotated[str, typer.Option()] = "",
    participants: Annotated[str, typer.Option(help="Comma-separated user ids")] = "",
) -> None:
    """Upload a recording (queued to WhisperX) or a transcript (extraction only)."""
    if path is None and transcript is None:
        typer.secho("give an audio path or --transcript", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    files: dict[str, Any] = {}
    if path is not None and not skip_asr:
        files["audio"] = (path.name, path.read_bytes())
    if transcript is not None:
        files["transcript"] = (transcript.name, transcript.read_bytes(), "application/json")
    c = _client()
    r = c.http.post(
        "/v1/meetings",
        data={"title": title or (path or transcript).stem, "participants": participants},
        files=files,
    )  # type: ignore[union-attr]
    c._raise(r)
    _echo_json(r.json())


@meeting_app.command("list")
def meeting_list() -> None:
    _echo_json(_client().get("/v1/meetings"))


@meeting_app.command("status")
def meeting_status(meeting_id: str) -> None:
    _echo_json(_client().get(f"/v1/meetings/{meeting_id}"))


# ---- standup -----------------------------------------------------------------------------------


@standup_app.command("draft")
def standup_draft(
    hours: Annotated[int, typer.Option()] = 24,
    github: Annotated[bool, typer.Option(help="Include GitHub activity via gh")] = False,
) -> None:
    """Print the sourced Done/Doing/Blocked skeleton for me (every line cites an event id)."""
    c = _client()
    evs = c.call_tool("events", {"since": f"{hours}h", "actor": "me", "limit": 500})["events"]
    items = c.call_tool("my_items", {"status": "open"})["items"]
    done, doing, blocked, questions, cited = [], [], [], [], []
    reported = set()
    for e in evs:
        p = e["payload"]
        if e["type"] == "action_item.closed":
            done.append(f"{p.get('note') or 'closed ' + p['item_id']} [{e['id']}]")
        elif e["type"] == "report.posted":
            doing.append(f"{p['text']} [{e['id']}]")
            if p.get("item_id"):
                reported.add(p["item_id"])
        elif e["type"] == "action_item.updated" and p.get("status") == "blocked":
            blocked.append(f"{p.get('note') or p['item_id']} [{e['id']}]")
        else:
            continue
        cited.append(e["id"])
    for it in items:
        if it["id"] not in reported:
            questions.append(
                f"Still working on '{it['title']}' ({it['id']})? No report in the last {hours}h."
            )
    gh_activity: list[dict[str, Any]] = []
    if github:
        from relayagents.connectors.github import GhIssueTracker

        login = c.whoami()["user"].get("github_login")
        if login:
            from datetime import UTC, datetime, timedelta

            try:
                gh_activity = asyncio.run(
                    GhIssueTracker().user_activity(
                        c.creds.user_id,
                        login,
                        since_iso=(datetime.now(UTC) - timedelta(hours=hours)).isoformat(),
                    )
                )
            except Exception as exc:
                questions.append(f"(gh unavailable: {exc})")
    _echo_json(
        {
            "user_id": c.creds.user_id,
            "done": done,
            "doing": doing,
            "blocked": blocked,
            "questions": questions,
            "cited_event_ids": cited,
            "github_activity": gh_activity,
        }
    )


@standup_app.command("submit")
def standup_submit(
    draft_file: Annotated[
        Path,
        typer.Argument(
            help="JSON produced by `relay standup draft` (optionally edited by the agent)"
        ),
    ],
) -> None:
    """Submit a draft under my posting mode (draft: DM with buttons; auto: post with attribution; off: nothing)."""
    c = _client()
    _echo_json(c.post("/v1/standups", json.loads(draft_file.read_text())))


# ---- approvals -----------------------------------------------------------------------------------

approvals_app = typer.Typer(
    help="Approvals requested of you (the Slack buttons are the usual path)."
)
app.add_typer(approvals_app, name="approvals")


@approvals_app.command("list")
def approvals_list(
    status: Annotated[
        str, typer.Option(help="pending | approved | denied | expired | all")
    ] = "pending",
) -> None:
    rows = _client().get("/v1/approvals", status=status)
    if not rows:
        typer.echo(f"no {status} approvals")
        return
    for r in rows:
        typer.echo(f"{r['id']}  [{r['status']:<8}] {r['requester']:<20} {r['action'][:80]}")


@approvals_app.command("approve")
def approvals_approve(
    approval_id: str,
    edit: Annotated[
        str | None, typer.Option(help="Replace the action text before approving")
    ] = None,
) -> None:
    _echo_json(
        _client().post(
            f"/v1/approvals/{approval_id}/resolve", {"decision": "approved", "edited_action": edit}
        )
    )


@approvals_app.command("deny")
def approvals_deny(approval_id: str, note: Annotated[str | None, typer.Option()] = None) -> None:
    _echo_json(
        _client().post(f"/v1/approvals/{approval_id}/resolve", {"decision": "denied", "note": note})
    )


# ---- replay / serve / worker / migrate (node-side) ---------------------------------------------


@app.command()
def replay(
    rebuild_graph: Annotated[
        bool, typer.Option("--rebuild-graph", help="Wipe and re-index the team graph from the log")
    ] = False,
    rebuild_projections: Annotated[
        bool,
        typer.Option(
            "--rebuild-projections", help="Rebuild action_items/decisions tables from the log"
        ),
    ] = False,
) -> None:
    """Rebuild derived stores from the event log. Proves the log is the source of truth.

    Runs where the team key and graph volume live: `docker compose exec relay-workers relay replay`.
    """
    from relayagents.api.app import build_services
    from relayagents.core import projections
    from relayagents.core.config import get_settings
    from relayagents.core.store import EventStore
    from relayagents.workers.jobs import rebuild_graph as rebuild_graph_job

    if not (rebuild_graph or rebuild_projections):
        rebuild_graph = rebuild_projections = True

    async def run() -> None:
        services = build_services(get_settings(), role="worker")
        if rebuild_projections:
            async with services.db.session() as session:
                n = await projections.rebuild(session, EventStore(session))
                await session.commit()
            typer.echo(f"projections rebuilt from {n} events")
        if rebuild_graph:
            n = await rebuild_graph_job({"services": services})
            typer.echo(
                f"graph and embeddings rebuilt from {n} events"
                if (services.memory or services.embedder)
                else "no memory backend or embedding model configured; nothing to rebuild"
            )
        await services.db.dispose()

    asyncio.run(run())


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    """Run relay-api."""
    import uvicorn

    uvicorn.run("relayagents.api.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command()
def worker(
    ingest: Annotated[bool, typer.Option(help="Run the ingest (WhisperX) worker instead")] = False,
) -> None:
    """Run relay-workers (or the ingest worker)."""
    from arq import run_worker

    if ingest:
        from relayagents.ingest.worker import WorkerSettings as Ingest

        run_worker(Ingest)  # type: ignore[arg-type]
    else:
        from relayagents.workers.main import WorkerSettings

        run_worker(WorkerSettings)  # type: ignore[arg-type]


@app.command()
def migrate(revision: str = "head") -> None:
    """Apply Alembic migrations."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "core" / "migrations")
    )
    command.upgrade(cfg, revision)


@app.command()
def health() -> None:
    """Check the Relay node."""
    import httpx

    url = (os.environ.get("RELAY_URL") or Credentials.load().url).rstrip("/")
    r = httpx.get(f"{url}/health", timeout=10)
    _echo_json(r.json())
    if r.json().get("status") != "ok":
        sys.exit(1)


register_tool_commands(app, _client)


if __name__ == "__main__":
    app()
