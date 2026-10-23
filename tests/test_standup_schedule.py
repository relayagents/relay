"""The workers' per-minute cron drafts and submits standups at each user's local time (ADR-0011)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from relayagents.core.config import Settings
from relayagents.core.models import UserRow
from relayagents.tools.context import Services
from relayagents.workers.standup import run_due_standups

# A fixed day in the past: events are stamped with the real clock, so "already ran today" must
# hold however far the real date has moved on from the scheduler's `now`.
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
