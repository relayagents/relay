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


def test_connect_guide_matches_setup_agent() -> None:
    # docs/connect-your-agent.md is followed by agents, word for word; keep its table true.
    text = (DOCS / "connect-your-agent.md").read_text()
    rows = dict(re.findall(r"^\| [^|]+ \| `([a-z-]+)` \| (.+?) \|$", text, flags=re.M))
    assert set(rows) == set(agent_setup.PROJECT_AGENTS)
    for agent, cell in rows.items():
        env = agent_setup.TOKEN_ENV.get(agent)
        assert (f"`{env}`" in cell) if env else cell.startswith("none"), agent
    skill = SKILL.read_text()
    assert "docs/connect-your-agent.md" in skill
    assert f"<{'|'.join(agent_setup.PROJECT_AGENTS)}>" in skill


MODEL_SERVER_VARS = ("OLLAMA_BASE_URL", "VLLM_BASE_URL", "VLLM_API_KEY")


def _compose_block(compose: str, header: str) -> str:
    """The lines under ``header`` up to the next line at the same or lower indent."""
    start = compose.index(header + "\n") + len(header) + 1
    indent = len(header) - len(header.lstrip()) + 1
    lines = []
    for line in compose[start:].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        lines.append(line)
    return "\n".join(lines)


def test_model_settings_are_documented() -> None:
    """docs/models.md covers every model setting and every local model server variable."""
    text = (DOCS / "models.md").read_text()
    env_example = (ROOT / ".env.example").read_text()
    for var in ("RELAY_EXTRACTION_MODEL", "RELAY_EMBEDDING_MODEL", "RELAY_EMBEDDING_DIM"):
        assert f"`{var}" in text, var
    for var in MODEL_SERVER_VARS:
        assert f"`{var}`" in text, var
        assert re.search(rf"^{var}=$", env_example, flags=re.M), var


def test_model_endpoints_reach_workers_only() -> None:
    """Model keys and model server URLs go to relay-workers, never relay-api (ADR-0002)."""
    compose = (ROOT / "docker-compose.yml").read_text()
    shared = _compose_block(compose, "x-relay-env: &relay-env")
    team = _compose_block(compose, "x-team-keys: &team-keys")
    api = _compose_block(compose, "  relay-api:")
    workers = _compose_block(compose, "  relay-workers:")
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", *MODEL_SERVER_VARS):
        assert f"  {var}: " in team, var
        assert var not in shared and var not in api, var
    assert "*team-keys" in workers and "*team-keys" not in api
    # Set-but-empty OPENAI_BASE_URL gives the OpenAI client an empty URL (docs/models.md).
    assert not re.search(r"^\s+OPENAI_BASE_URL:", compose, flags=re.M)
