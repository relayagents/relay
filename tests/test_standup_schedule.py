"""The workers' per-minute cron drafts and submits standups at each user's local time (ADR-0011)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from relayagents.api.routes.users import AddUserIn, create_user_with_tokens
from relayagents.core.config import Settings
from relayagents.core.events import Actor, Event, StandupPosted
from relayagents.core.models import ApprovalRow, UserRow
from relayagents.core.store import EventStore
from relayagents.tools.context import Services
from relayagents.workers.standup import run_due_standups
from tests.conftest import auth

# A fixed day, not today: the scheduler stamps what it records with its own `now`, so these tests
# do not depend on the real clock.
DAY = datetime(2026, 6, 15, tzinfo=UTC)


async def _set(services: Services, uid: str, **fields: str) -> None:
    async with services.db.session() as session:
        user = await session.get(UserRow, uid)
        assert user is not None
        for k, v in fields.items():
            setattr(user, k, v)
        await session.commit()


async def test_due_users_get_a_draft_once(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    await _set(services, "grace", timezone="America/New_York", standup_time="09:00")
    at = DAY.replace(hour=13)  # 09:00 in New York (EDT); ada and linus are 09:00 UTC
    assert await run_due_standups(services, now=at) == ["grace"]
    dm = services.chat.dms[-1]  # type: ignore[union-attr]
    assert dm["user_id"] == "grace" and dm["text"] == "Your standup draft is ready."
    assert dm["blocks"][1]["elements"][0]["action_id"] == "standup_approve"
    # the worker restarts inside the same minute: nothing new
    assert await run_due_standups(services, now=at) == []
    assert len(services.chat.dms) == 1  # type: ignore[union-attr]
    # a minute later: not due
    assert await run_due_standups(services, now=at.replace(minute=1)) == []


async def test_auto_mode_posts_with_relay_attribution(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    await _set(services, "linus", standup_mode="auto", standup_time="17:30")
    at = DAY.replace(hour=17, minute=30)
    assert await run_due_standups(services, now=at) == ["linus"]
    post = services.chat.posts[-1]  # type: ignore[union-attr]
    assert post["channel"] == "C_TEAM" and post["attribution"] == "posted by Relay for Linus"
    assert await run_due_standups(services, now=at) == []
    assert len(services.chat.posts) == 1  # type: ignore[union-attr]


async def test_off_mode_and_bad_timezone(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    await _set(services, "ada", standup_mode="off")
    await _set(services, "grace", timezone="Mars/Olympus")  # falls back to UTC, does not crash
    at = DAY.replace(hour=9)
    assert set(await run_due_standups(services, now=at)) == {"grace", "linus"}


async def test_one_failing_user_does_not_stop_the_others(  # type: ignore[no-untyped-def]
    services: Services, team, monkeypatch
) -> None:
    real_dm = services.chat.dm  # type: ignore[union-attr]

    async def dm(user_id: str, text: str, **kw: Any):  # type: ignore[no-untyped-def]
        if user_id == "ada":
            raise LookupError("ada has no slack_user_id")
        return await real_dm(user_id, text, **kw)

    monkeypatch.setattr(services.chat, "dm", dm)
    assert set(await run_due_standups(services, now=DAY.replace(hour=9))) == {"grace", "linus"}


@pytest.mark.parametrize(("raw", "expected"), [("", False), ("true", True)])
def test_slack_buttons_accepts_compose_empty_expansion(
    monkeypatch, raw: str, expected: bool
) -> None:  # type: ignore[no-untyped-def]
    # compose sets RELAY_SLACK_BUTTONS: ${SLACK_APP_TOKEN:+true}, which is "" without an app token
    monkeypatch.setenv("RELAY_SLACK_BUTTONS", raw)
    assert Settings(_env_file=None).slack_buttons is expected  # type: ignore[call-arg]


def test_workers_post_buttons_without_opening_socket_mode(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from relayagents.api.app import build_services

    worker = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/w.db",
        slack_bot_token="xoxb-test",
        slack_buttons=True,
        _env_file=None,  # type: ignore[call-arg]
    )
    assert worker.slack_buttons_enabled and not worker.slack_socket_mode_enabled
    assert build_services(worker, role="worker").chat.supports_actions  # type: ignore[union-attr]
    api = Settings(slack_bot_token="xoxb-test", slack_app_token="xapp-test", _env_file=None)  # type: ignore[call-arg]
    assert api.slack_buttons_enabled and api.slack_socket_mode_enabled
    plain = Settings(slack_bot_token="xoxb-test", _env_file=None)  # type: ignore[call-arg]
    assert not plain.slack_buttons_enabled


def test_the_workers_run_the_scheduler_every_minute() -> None:
    from relayagents.workers import jobs
    from relayagents.workers.main import WorkerSettings

    (job,) = [c for c in WorkerSettings.cron_jobs if c.coroutine is jobs.scheduled_standups]
    assert job.minute is None and job.hour is None and job.second == 0
    assert job.run_at_startup is False  # a restart must not re-run the minute it already ran


def test_compose_lets_only_the_workers_post_buttons_for_relay_api() -> None:
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    workers = compose.split("\n  relay-workers:\n", 1)[1].split("\n  relay-", 1)[0]
    assert "RELAY_SLACK_BUTTONS: ${SLACK_APP_TOKEN:+true}" in workers
    assert "RELAY_SLACK_APP_TOKEN" not in workers  # Socket Mode stays in relay-api alone


def test_draft_questions_are_relays_not_the_agents() -> None:
    from relayagents.workers.standup import StandupDraft, draft_blocks

    draft = StandupDraft(user_id="grace", questions=["Still working on 'x' (item_1)?"])
    text = draft_blocks(draft)[0]["text"]["text"]
    assert "*Open questions*" in text and "from your agent" not in text


async def test_a_late_run_catches_up_within_two_hours(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    # a busy or restarting worker runs the 09:00 tick at 09:05: ada and linus still get theirs
    await _set(services, "grace", standup_time="06:30")  # 2h35 ago: too late, skipped today
    assert set(await run_due_standups(services, now=DAY.replace(hour=9, minute=5))) == {
        "ada",
        "linus",
    }
    assert await run_due_standups(services, now=DAY.replace(hour=10, minute=59)) == []


async def test_an_evening_approval_does_not_suppress_the_next_local_day(  # type: ignore[no-untyped-def]
    services: Services, team
) -> None:
    await _set(services, "grace", timezone="America/Los_Angeles")  # 09:00 PDT is 16:00 UTC
    assert await run_due_standups(services, now=DAY.replace(hour=16)) == ["grace"]
    # she approves at 17:30 PDT, which is already the next day in UTC
    next_utc_day = DAY + timedelta(days=1)
    async with services.db.session() as session:
        await EventStore(session).append(
            Event.new(
                StandupPosted(user_id="grace", mode="draft"),
                actor=Actor.human("grace"),
                source="slack",
                thread_id=f"standup:grace:{next_utc_day:%Y-%m-%d}",
                ts=next_utc_day.replace(hour=0, minute=30),
            )
        )
        await session.commit()
    assert await run_due_standups(services, now=next_utc_day.replace(hour=16)) == ["grace"]


async def test_a_standup_time_in_the_spring_forward_gap_still_runs(
    services: Services, team
) -> None:  # type: ignore[no-untyped-def]
    # 02:30 does not exist in New York on 2026-03-08; 07:00 UTC is 03:00 EDT
    await _set(services, "grace", timezone="America/New_York", standup_time="02:30")
    at = datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert await run_due_standups(services, now=at) == ["grace"]


async def test_draft_mode_needs_someone_to_dm(services: Services, team) -> None:  # type: ignore[no-untyped-def]
    await create_user_with_tokens(
        services, AddUserIn(id="nora", display_name="Nora"), issued_by=Actor.system("relay.test")
    )
    at = DAY.replace(hour=9)
    assert "nora" not in await run_due_standups(services, now=at)  # no Slack id: nothing opened
    await _set(services, "linus", standup_mode="auto")
    services.chat = None  # no Slack at all: drafts would reach nobody; auto still records
    assert await run_due_standups(services, now=at + timedelta(days=1)) == ["linus"]
    async with services.db.session() as session:
        offered = (await session.scalars(select(ApprovalRow.requested_of))).all()
    assert "nora" not in offered and offered.count("ada") == 1


@pytest.mark.parametrize(
    "body",
    [
        {"timezone": "Mars/Olympus"},
        {"timezone": "America/NewYork"},
        {"timezone": "localtime"},  # a file on the writer's machine, not an IANA name
        {"timezone": "posixrules"},
        {"standup_time": "25:00"},
    ],
)
async def test_bad_schedule_settings_are_refused_on_write(client, team, body) -> None:  # type: ignore[no-untyped-def]
    r = await client.patch("/v1/me", json=body, headers=auth(team["grace"]["human"]))
    assert r.status_code == 422, r.text
    r = await client.post(
        "/v1/users",
        json={"id": "mallory", "display_name": "M", "timezone": "Mars/Olympus"},
        headers=auth(team["ada"]["human"]),
    )
    assert r.status_code == 422, r.text
    ok = await client.patch(
        "/v1/me",
        json={"timezone": "Europe/Paris", "standup_time": "23:59"},
        headers=auth(team["grace"]["human"]),
    )
    assert ok.status_code == 200, ok.text


def test_add_user_cli_refuses_an_unknown_timezone_cleanly() -> None:
    from typer.testing import CliRunner

    from relayagents.cli import main

    result = CliRunner().invoke(main.app, ["add-user", "nora", "--timezone", "Mars/Olympus"])
    assert result.exit_code == 1 and "unknown timezone 'Mars/Olympus'" in result.output
    assert "Traceback" not in result.output
