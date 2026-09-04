"""Relay ↔ Hermes bridge. Makes a Hermes container satisfy docs/agent-contract.md without
depending on Hermes internals beyond "run one prompt headlessly from the CLI".

Loop 1 (inbox): long-poll ``GET /a2a/inbox?wait=25`` with the agent token; for each task mark it
``working``, run Hermes with the task text plus the relay skill, then post the reply as
``completed`` (or ``failed``). Loop 2 (standup): once a day at the user's ``standup_time`` run
``relay standup draft``, let Hermes tidy the wording (not the facts), then ``relay standup submit``.

Configuration (env): RELAY_URL, RELAY_TOKEN, RELAY_USER, HERMES_CMD (default: ``hermes chat -q``),
HERMES_TIMEOUT_S (default 1200), RELAY_BRIDGE_ONESHOT=1 to process the inbox once and exit.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

RELAY_URL = os.environ["RELAY_URL"].rstrip("/")
TOKEN = os.environ["RELAY_TOKEN"]
HERMES_CMD = shlex.split(os.environ.get("HERMES_CMD", "hermes chat -q"))
TIMEOUT = int(os.environ.get("HERMES_TIMEOUT_S", "1200"))
SKILL = Path.home() / ".hermes" / "skills" / "relay" / "SKILL.md"

client = httpx.Client(base_url=RELAY_URL, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)


def log(msg: str, **kw: object) -> None:
    print(json.dumps({"ts": datetime.now(UTC).isoformat(), "msg": msg, **kw}), flush=True)


def run_hermes(prompt: str) -> tuple[int, str]:
    skill = SKILL.read_text() if SKILL.exists() else ""
    full = f"{skill}\n\n---\n\n{prompt}" if skill else prompt
    try:
        proc = subprocess.run(
            [*HERMES_CMD, full], capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except FileNotFoundError:
        return 127, f"hermes binary not found; set HERMES_CMD (tried {HERMES_CMD[0]})"
    except subprocess.TimeoutExpired:
        return 124, f"hermes timed out after {TIMEOUT}s"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        out = f"{out}\n{(proc.stderr or '').strip()}".strip()
    return proc.returncode, out[-8000:]


def handle_task(task: dict) -> None:
    task_id = task["id"]
    # Label every message with its side so a later "user" message cannot pose as the original ask.
    text = "\n\n".join(
        f"[{m.get('role', 'user')} message {i + 1}]\n"
        + "\n".join(p.get("text", "") for p in m.get("parts", []) if p.get("text"))
        for i, m in enumerate(task.get("history", []))
    )
    log("task.start", task_id=task_id)
    client.post(f"/a2a/tasks/{task_id}", json={"state": "working"})
    rc, reply = run_hermes(
        f"A teammate's agent (or Relay's PM) sent you this task via Relay. Task id {task_id}, thread {task.get('contextId')}.\n\n{text}\n\n"
        "Respond with what you did or the answer. Use the `relay` CLI for anything that needs team memory; "
        "never take an external action without `relay request-approval` first."
    )
    state = "completed" if rc == 0 else "failed"
    client.post(
        f"/a2a/tasks/{task_id}",
        json={
            "state": state,
            "message": {"role": "agent", "parts": [{"text": reply or "(no output)"}]},
        },
    )
    log("task.done", task_id=task_id, state=state)


def inbox_once() -> int:
    r = client.get("/a2a/inbox", params={"wait": 25})
    r.raise_for_status()
    tasks = r.json()
    for t in tasks:
        try:
            handle_task(t)
        except Exception as exc:
            log("task.error", task_id=t.get("id"), error=str(exc))
    return len(tasks)


def standup_due(last_run: str | None) -> bool:
    me = client.get("/v1/me").json()["user"]
    if me.get("standup_mode", "draft") == "off":
        return False
    now = datetime.now(ZoneInfo(me.get("timezone") or "UTC"))
    hh, mm = (me.get("standup_time") or "09:00").split(":")
    today = now.strftime("%Y-%m-%d")
    return last_run != today and (now.hour, now.minute) >= (int(hh), int(mm))


def do_standup() -> None:
    draft = subprocess.run(
        ["relay", "standup", "draft", "--github"], capture_output=True, text=True, check=False
    )
    if draft.returncode != 0:
        log("standup.draft_failed", stderr=draft.stderr[-500:])
        return
    data = json.loads(draft.stdout)
    rc, reply = run_hermes(
        "Rewrite this standup draft for clarity. Keep every [evt_...] citation. Do not add facts that have no citation; "
        "if something is unclear, keep it in `questions`. Phrase blockers by topic, not by person. "
        f"Return ONLY the JSON object with the same keys.\n\n{json.dumps(data)}"
    )
    try:
        edited = json.loads(reply[reply.index("{") : reply.rindex("}") + 1]) if rc == 0 else data
        edited["user_id"] = data["user_id"]
        edited.setdefault("cited_event_ids", data["cited_event_ids"])
    except Exception:
        edited = data
    path = Path.home() / ".config" / "relay" / "standup-draft.json"
    path.write_text(json.dumps(edited))
    sub = subprocess.run(
        ["relay", "standup", "submit", str(path)], capture_output=True, text=True, check=False
    )
    log("standup.submitted", rc=sub.returncode, out=sub.stdout[-300:], err=sub.stderr[-300:])


def main() -> int:
    log("bridge.start", url=RELAY_URL, hermes=HERMES_CMD)
    if os.environ.get("RELAY_BRIDGE_ONESHOT"):
        return 0 if inbox_once() >= 0 else 1
    last_standup: str | None = None
    while True:
        try:
            inbox_once()
            if standup_due(last_standup):
                do_standup()
                last_standup = datetime.now(UTC).strftime("%Y-%m-%d")
        except httpx.HTTPStatusError as exc:
            log("bridge.http_error", status=exc.response.status_code, body=exc.response.text[:200])
            time.sleep(10)
        except Exception as exc:
            log("bridge.error", error=str(exc))
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
