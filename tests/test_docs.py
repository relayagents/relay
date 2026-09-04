"""Docs that mirror code stay in sync."""

from __future__ import annotations

import re
from pathlib import Path

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
