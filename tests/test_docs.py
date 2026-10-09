"""Docs that mirror code stay in sync."""

from __future__ import annotations

import json
import re
from pathlib import Path

from relayagents.cli import agent_setup
from relayagents.core.events import EVENT_TYPES
from relayagents.core.permissions import DEFAULT_POLICY
from relayagents.tools import TOOLS

DOCS = Path(__file__).resolve().parents[1] / "docs"
ROOT = DOCS.parent


def test_permissions_table_matches_code() -> None:
    text = (DOCS / "permissions.md").read_text()
    rows = dict(re.findall(r"^\| `([a-z_.]+)` \| (auto|approve|forbid) \|", text, flags=re.M))
    assert rows == DEFAULT_POLICY


def test_data_model_lists_every_event_type() -> None:
    text = (DOCS / "data-model.md").read_text()
    for t in EVENT_TYPES:
        assert f"| `{t}` |" in text, t


def test_readme_lists_every_tool() -> None:
    text = (ROOT / "README.md").read_text()
    for spec in TOOLS:
        assert f"`{spec.name}" in text, spec.name


def test_adr_index_matches_files() -> None:
    index = (DOCS / "adr" / "README.md").read_text()
    for f in sorted((DOCS / "adr").glob("0*.md")):
        assert f.name in index, f.name


SKILL = ROOT / "src" / "relayagents" / "agent_plugin" / "skills" / "relay" / "SKILL.md"


def test_agent_skill_lists_every_tool() -> None:
    text = SKILL.read_text()
    table = set(re.findall(r"^\| `([a-z_]+)` \|", text, flags=re.M))
    assert table == {spec.name for spec in TOOLS}


def test_agent_skill_approval_keys_match_policy() -> None:
    approvals = SKILL.read_text().split("## Approvals", 1)[1]
    keys = set(re.findall(r"`([a-z_]+(?:\.[a-z_]+)+)`", approvals))
    assert keys == {k for k, v in DEFAULT_POLICY.items() if v == "approve"}


def test_agent_plugin_is_wired_to_the_marketplace() -> None:
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    (entry,) = market["plugins"]
    plugin_dir = (ROOT / entry["source"]).resolve()
    manifest = json.loads((plugin_dir / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == entry["name"] == "relay"
    assert manifest["defaultEnabled"] is False  # opt-in per project (ADR-0010)
    assert f"{entry['name']}@{market['name']}" == agent_setup.PLUGIN_ID
    assert plugin_dir == ROOT / agent_setup.MARKETPLACE_SPARSE[1]
    assert all((ROOT / p).exists() for p in agent_setup.MARKETPLACE_SPARSE)
    assert Path(str(agent_setup.skill_source() / "SKILL.md")) == SKILL
    front = re.match(r"---\n(.*?)\n---\n", SKILL.read_text(), flags=re.S)
    assert front and "name: relay\n" in front.group(1) + "\n"
    assert len(front.group(1)) <= 1024
