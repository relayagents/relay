"""`relay setup-agent`: per-project plans, config merges that keep secrets out, skill install."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from relayagents.cli import agent_setup as s
from relayagents.cli import main

URL = "https://relay.example.dev"
TOKEN = "rly_agent_secret_123"


def _runs(plan: s.Plan) -> list[s.Run]:
    return [st for st in plan.steps if isinstance(st, s.Run)]


# ---- Claude Code -------------------------------------------------------------------------------


def test_claude_code_plan_is_local_to_the_project(tmp_path: Path) -> None:
    plan = s.plan("claude-code", URL, TOKEN, tmp_path)
    runs = _runs(plan)
    assert [r.argv[:3] for r in runs] == [
        ["claude", "plugin", "marketplace"],
        ["claude", "plugin", "install"],
        ["claude", "plugin", "enable"],
        ["claude", "mcp", "remove"],
        ["claude", "mcp", "add"],
    ]
    assert all(r.cwd == tmp_path for r in runs)
    # Everything that enables something is scoped to this folder; nothing is user-wide.
    for r in runs[1:]:
        assert r.argv[r.argv.index("--scope") + 1] == "local"
    assert runs[0].argv[-3:] == ["--sparse", *s.MARKETPLACE_SPARSE]
    assert s.PLUGIN_ID in runs[1].argv and s.PLUGIN_ID in runs[2].argv
    assert runs[3].allow_failure  # removing a server that is not there is fine
    add = runs[4]
    assert add.argv[-2:] == ["--header", f"Authorization: Bearer {TOKEN}"]
    assert f"{URL}/mcp" in add.argv
    assert [r for r in runs if any(TOKEN in a for a in r.argv)] == [add]


def test_local_marketplace_path_is_not_sparse(tmp_path: Path) -> None:
    plan = s.plan("claude-code", URL, TOKEN, tmp_path, marketplace=str(tmp_path))
    assert "--sparse" not in _runs(plan)[0].argv


def test_describe_masks_the_token_only_when_asked(tmp_path: Path) -> None:
    add = _runs(s.plan("claude-code", URL, TOKEN, tmp_path))[-1]
    assert TOKEN in s.describe(add, reveal=True)
    masked = s.describe(add, reveal=False)
    assert TOKEN not in masked and "Bearer ***" in masked


def test_apply_run_reports_failures_without_the_token(tmp_path: Path) -> None:
    add = _runs(s.plan("claude-code", URL, TOKEN, tmp_path))[-1]

    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", f"bad header Bearer {TOKEN}")

    with pytest.raises(s.SetupError) as err:
        s.apply(add, runner=failing)
    assert TOKEN not in str(err.value) and "***" in str(err.value)

    remove = s.Run(["claude", "mcp", "remove", "relay"], tmp_path, allow_failure=True)
    s.apply(remove, runner=failing)  # tolerated

    enable = next(
        r for r in _runs(s.plan("claude-code", URL, TOKEN, tmp_path)) if "enable" in r.argv
    )

    def already(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", 'Plugin "relay" is already enabled')

    s.apply(enable, runner=already)  # a rerun is not an error
    with pytest.raises(s.SetupError):
        s.apply(enable, runner=failing)  # but a real failure still is

    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])

    with pytest.raises(s.SetupError, match="not on your PATH"):
        s.apply(remove, runner=missing)


# ---- Codex ---------------------------------------------------------------------------------------


def test_codex_config_holds_no_secret_and_keeps_other_tables() -> None:
    existing = (
        'model = "o5"\n\n[mcp_servers.github]\ncommand = "gh-mcp"\n\n'
        '[mcp_servers.relay]\nurl = "https://old/mcp"\nbearer_token_env_var = "RELAY_TOKEN"\n\n'
        '[mcp_servers.relay.env_http_headers]\nX = "Y"\n\n[profiles.fast]\nmodel = "o5-mini"\n'
    )
    merged = s.merge_codex_config(existing, URL, "RELAY_CODEX_TOKEN")
    data = tomllib.loads(merged)
    assert data["mcp_servers"]["relay"] == {
        "url": f"{URL}/mcp",
        "bearer_token_env_var": "RELAY_CODEX_TOKEN",
    }
    assert data["mcp_servers"]["github"] == {"command": "gh-mcp"}
    assert data["profiles"]["fast"] == {"model": "o5-mini"}
    assert data["model"] == "o5"
    assert TOKEN not in merged
    assert s.merge_codex_config(merged, URL, "RELAY_CODEX_TOKEN") == merged  # idempotent


def test_codex_config_new_file() -> None:
    merged = s.merge_codex_config("", URL, "RELAY_CODEX_TOKEN")
    assert merged.startswith("[mcp_servers.relay]\n")


@pytest.mark.parametrize(
    "existing",
    ['mcp_servers.relay.url = "https://x/mcp"\n', 'mcp_servers = { relay = { url = "x" } }\n'],
)
def test_codex_config_refuses_relay_it_cannot_replace(existing: str) -> None:
    with pytest.raises(s.SetupError, match=r"not a \[mcp_servers\.relay\] table"):
        s.merge_codex_config(existing, URL, "RELAY_CODEX_TOKEN")


def test_codex_config_refuses_invalid_toml() -> None:
    with pytest.raises(s.SetupError, match="not valid TOML"):
        s.merge_codex_config("[unclosed\n", URL, "RELAY_CODEX_TOKEN")


def test_codex_plan(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "proj"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('[mcp_servers.relay]\nurl = "x"\n')
    plan = s.plan("codex", URL, TOKEN, project, home=home)
    write = next(st for st in plan.steps if isinstance(st, s.WriteFile))
    assert write.path == project / ".codex" / "config.toml"
    assert TOKEN not in write.content
    assert [st.dest for st in plan.steps if isinstance(st, s.InstallSkill)] == [
        home / ".agents" / "skills" / "relay"
    ]
    notes = "\n".join(st.text for st in plan.steps if isinstance(st, s.Note))
    assert "connects Relay in every project" in notes  # the old global entry is called out
    assert f"export RELAY_CODEX_TOKEN={TOKEN}" in notes
    assert "RELAY_TOKEN=" not in notes.replace("RELAY_CODEX_TOKEN=", "")


# ---- OpenCode ------------------------------------------------------------------------------------


def test_opencode_config_reads_the_token_from_env() -> None:
    existing = json.dumps({"model": "x", "mcp": {"other": {"type": "local"}}})
    merged = s.merge_opencode_config(existing, URL, "RELAY_OPENCODE_TOKEN")
    data = json.loads(merged)
    assert data["model"] == "x" and data["mcp"]["other"] == {"type": "local"}
    assert data["mcp"]["relay"] == {
        "type": "remote",
        "url": f"{URL}/mcp",
        "enabled": True,
        "headers": {"Authorization": "Bearer {env:RELAY_OPENCODE_TOKEN}"},
    }
    fresh = json.loads(s.merge_opencode_config("", URL, "RELAY_OPENCODE_TOKEN"))
    assert fresh["$schema"] == "https://opencode.ai/config.json"


def test_opencode_config_refuses_jsonc() -> None:
    with pytest.raises(s.SetupError, match="not plain JSON"):
        s.merge_opencode_config('{ // comment\n "a": 1 }', URL, "RELAY_OPENCODE_TOKEN")


# ---- Cursor --------------------------------------------------------------------------------------


def test_cursor_config_reads_the_token_from_env() -> None:
    existing = json.dumps({"mcpServers": {"other": {"command": "npx", "args": ["x"]}}})
    merged = s.merge_cursor_config(existing, URL, "RELAY_CURSOR_TOKEN")
    data = json.loads(merged)
    assert data["mcpServers"]["other"] == {"command": "npx", "args": ["x"]}
    assert data["mcpServers"]["relay"] == {
        "url": f"{URL}/mcp",
        "headers": {"Authorization": "Bearer ${env:RELAY_CURSOR_TOKEN}"},
    }
    assert json.loads(s.merge_cursor_config("", URL, "RELAY_CURSOR_TOKEN")) == {
        "mcpServers": {"relay": data["mcpServers"]["relay"]}
    }
    with pytest.raises(s.SetupError, match="not plain JSON"):
        s.merge_cursor_config("{ // c\n }", URL, "RELAY_CURSOR_TOKEN")
    with pytest.raises(s.SetupError, match='"mcpServers" key that is not an object'):
        s.merge_cursor_config('{"mcpServers": []}', URL, "RELAY_CURSOR_TOKEN")


def test_cursor_plan(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "proj"
    (home / ".cursor").mkdir(parents=True)
    (home / ".cursor" / "mcp.json").write_text('{"mcpServers": {"relay": {"url": "x"}}}')
    plan = s.plan("cursor", URL, TOKEN, project, home=home)
    write = next(st for st in plan.steps if isinstance(st, s.WriteFile))
    assert write.path == project / ".cursor" / "mcp.json"
    assert TOKEN not in write.content
    assert [st.dest for st in plan.steps if isinstance(st, s.InstallSkill)] == [
        home / ".agents" / "skills" / "relay"
    ]
    notes = "\n".join(st.text for st in plan.steps if isinstance(st, s.Note))
    assert "connects Relay in every project" in notes
    assert f"export RELAY_CURSOR_TOKEN={TOKEN}" in notes


# ---- skill install -------------------------------------------------------------------------------


def test_install_skill_copies_the_packaged_skill(tmp_path: Path) -> None:
    dest = tmp_path / ".agents" / "skills" / "relay"
    s.install_skill(dest)
    packaged = (s.skill_source() / "SKILL.md").read_text()
    assert (dest / "SKILL.md").read_text() == packaged
    (dest / "SKILL.md").write_text(packaged.replace("# Relay", "# Relay (old)"))
    s.install_skill(dest)  # an earlier copy of the same skill is replaced
    assert (dest / "SKILL.md").read_text() == packaged
    assert [p.name for p in dest.parent.iterdir()] == ["relay"]  # no staging dirs left behind


def test_install_skill_refuses_someone_elses_folder(tmp_path: Path) -> None:
    dest = tmp_path / "relay"
    dest.mkdir()
    (dest / "SKILL.md").write_text("---\nname: relay-race\ndescription: x\n---\n")
    with pytest.raises(s.SetupError, match="not Relay's skill"):
        s.install_skill(dest)
    assert "relay-race" in (dest / "SKILL.md").read_text()


def test_project_root_is_the_git_work_tree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "pkg").mkdir()
    assert s.project_root(tmp_path / "pkg") == tmp_path.resolve()
    outside = tmp_path.parent / f"{tmp_path.name}-not-a-repo"
    outside.mkdir()
    assert s.project_root(outside) == outside.resolve()


def test_project_root_ignores_inherited_git_paths(tmp_path, monkeypatch) -> None:
    other, project = tmp_path / "other", tmp_path / "proj"
    for repo in (other, project):
        repo.mkdir()
        _git(repo, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert s.project_root(project) == project.resolve()


# ---- the command ---------------------------------------------------------------------------------


class _FakeClient:
    """Stands in for RelayClient: mints, lists, and revokes tokens in memory."""

    def __init__(self) -> None:
        self.creds = SimpleNamespace(url=URL, token="rly_human_token")
        self.minted: list[dict] = []
        self.revoked: list[str] = []
        self.existing: list[dict] = []

    def post(self, path: str, body: dict) -> dict:
        assert path == "/v1/tokens"
        self.minted.append(body)
        return {"token": TOKEN, "token_id": "tok_new"}

    def get(self, path: str) -> list[dict]:
        assert path == "/v1/tokens"
        new = [
            {"token_id": "tok_new", "label": m["label"], "revoked_at": None} for m in self.minted
        ]
        return self.existing + new

    def delete(self, path: str) -> dict:
        self.revoked.append(path.rsplit("/", 1)[1])
        return {}


def test_setup_agent_codex_writes_project_config(tmp_path, monkeypatch) -> None:
    fake = _FakeClient()
    project = tmp_path / "proj"
    project.mkdir()
    label = s.token_label("codex", project.resolve())
    fake.existing = [
        {"token_id": "tok_old", "label": label, "revoked_at": None},  # this project, earlier run
        {"token_id": "tok_gone", "label": label, "revoked_at": "2026-10-01T00:00:00Z"},
        {"token_id": "tok_other", "label": "agent:codex:elsewhere:0123456789", "revoked_at": None},
    ]
    monkeypatch.setattr(main, "_client", lambda: fake)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(
        main.app, ["setup-agent", "codex", "--project", str(project), "--write"]
    )
    assert result.exit_code == 0, result.output
    assert fake.minted == [{"label": label, "actor_kind": "agent", "harness": "codex"}]
    assert fake.revoked == ["tok_old"]  # only the token this run replaces
    config = (project / ".codex" / "config.toml").read_text()
    assert TOKEN not in config and "rly_human_token" not in config
    assert (tmp_path / "home" / ".agents" / "skills" / "relay" / "SKILL.md").exists()
    assert f"export RELAY_CODEX_TOKEN={TOKEN}" in result.output


def test_setup_agent_preview_changes_nothing(tmp_path, monkeypatch) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(main, "_client", lambda: fake)
    monkeypatch.setattr(s, "project_root", lambda p: p)

    def no_subprocess(*a, **kw):
        raise AssertionError("printing must not run anything")

    monkeypatch.setattr(s.subprocess, "run", no_subprocess)
    result = CliRunner().invoke(
        main.app, ["setup-agent", "claude-code", "--project", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert fake.minted == [] and fake.revoked == []
    assert f"Authorization: Bearer {main.TOKEN_PLACEHOLDER}" in result.output
    assert "no token was minted" in result.output


def test_setup_agent_claude_code_write_runs_the_steps(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "_client", _FakeClient)
    monkeypatch.setattr(s, "project_root", lambda p: p)
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(s.subprocess, "run", fake_run)
    result = CliRunner().invoke(
        main.app, ["setup-agent", "claude-code", "--project", str(tmp_path), "--write"]
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 5 and calls[-1][:3] == ["claude", "mcp", "add"]
    assert TOKEN not in result.output  # applied commands are echoed masked


def test_setup_agent_revokes_the_new_token_when_a_step_fails(tmp_path, monkeypatch) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(main, "_client", lambda: fake)
    monkeypatch.setattr(s, "project_root", lambda p: p)

    def fail_mcp_add(argv, **kw):
        return subprocess.CompletedProcess(argv, 1 if argv[1:3] == ["mcp", "add"] else 0, "", "no")

    monkeypatch.setattr(s.subprocess, "run", fail_mcp_add)
    result = CliRunner().invoke(
        main.app, ["setup-agent", "claude-code", "--project", str(tmp_path), "--write"]
    )
    assert result.exit_code == 1
    assert fake.revoked == ["tok_new"]


def test_setup_agent_mints_no_token_when_the_config_cannot_be_merged(tmp_path, monkeypatch) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(main, "_client", lambda: fake)
    monkeypatch.setattr(s, "project_root", lambda p: p)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("[broken\n")
    result = CliRunner().invoke(
        main.app, ["setup-agent", "codex", "--project", str(tmp_path), "--write"]
    )
    assert result.exit_code == 1 and "not valid TOML" in result.output
    assert fake.minted == []


@pytest.mark.parametrize("agent", s.PROJECT_AGENTS)
def test_home_folder_is_not_a_project(tmp_path: Path, agent: str) -> None:
    # From ~, Codex's project config would be its user-wide ~/.codex/config.toml.
    with pytest.raises(s.SetupError, match="not a project"):
        s.plan(agent, URL, TOKEN, tmp_path, home=tmp_path)
    with pytest.raises(s.SetupError, match="not a project"):
        s.plan(agent, URL, TOKEN, Path("/"), home=tmp_path)


def test_symlinked_skill_folder_is_refused_at_planning(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "proj"
    skills = home / ".agents" / "skills"
    skills.mkdir(parents=True)
    (tmp_path / "dotfiles").mkdir()
    (skills / "relay").symlink_to(tmp_path / "dotfiles")
    with pytest.raises(s.SetupError, match="symlink"):
        s.plan("codex", URL, TOKEN, project, home=home)


def test_token_label_is_per_project_and_fits_the_api(tmp_path: Path) -> None:
    import re

    a, b = tmp_path / "team repo!", tmp_path / "other"
    label = s.token_label("claude-code", a)
    assert label == s.token_label("claude-code", a)
    assert label != s.token_label("claude-code", b)
    assert label != s.token_label("codex", a)
    assert re.fullmatch(r"[\w@.:-]{1,64}", label)  # TokenIn.label in api/routes/users.py
    long = s.token_label("opencode", tmp_path / ("x" * 200))
    assert len(long) <= 64


def test_token_label_follows_a_shared_env_file(tmp_path: Path) -> None:
    a, b, home = tmp_path / "a", tmp_path / "b", tmp_path / "home"
    profile = home / ".zshrc"
    # A profile serves every project, so its token is labelled by the profile...
    assert s.token_label("codex", a, profile) == s.token_label("codex", b, profile)
    assert s.token_label("codex", a, profile) != s.token_label("codex", a)
    assert s.token_label("codex", a, profile) != s.token_label("opencode", a, profile)
    # ...while a file inside the project keeps the per-project label.
    assert s.token_label("codex", a, a / ".envrc") == s.token_label("codex", a)


def test_setup_agent_shared_profile_revokes_the_token_it_overwrites(tmp_path, monkeypatch) -> None:
    fake = _FakeClient()
    home = tmp_path / "home"
    profile = home / ".zshrc"
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    shared = s.token_label("codex", first, profile)
    fake.existing = [{"token_id": "tok_from_first", "label": shared, "revoked_at": None}]
    monkeypatch.setattr(main, "_client", lambda: fake)
    monkeypatch.setattr(s, "project_root", lambda p: p)
    monkeypatch.setenv("HOME", str(home))
    args = ["setup-agent", "codex", "--project", str(second), "--env-file", str(profile)]
    result = CliRunner().invoke(main.app, [*args, "--write"])
    assert result.exit_code == 0, result.output
    assert fake.minted[0]["label"] == shared
    assert fake.revoked == ["tok_from_first"]  # overwritten in the profile, so revoked


def test_setup_agent_relative_env_file_is_in_the_project(tmp_path, monkeypatch) -> None:
    fake = _FakeClient()
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    (project / ".git" / "info" / "exclude").write_text(".envrc\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(main, "_client", lambda: fake)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    args = ["setup-agent", "codex", "--project", str(project), "--env-file", ".envrc"]
    result = CliRunner().invoke(main.app, [*args, "--write"])
    assert result.exit_code == 0, result.output
    assert (project / ".envrc").exists() and not (elsewhere / ".envrc").exists()


def test_setup_agent_rejects_unknown_agent() -> None:
    result = CliRunner().invoke(main.app, ["setup-agent", "windsurf"])
    assert result.exit_code == 1
    assert "unknown agent" in result.output


def test_hermes_config_carries_the_minted_token(tmp_path: Path) -> None:
    (step,) = s.plan("hermes", URL, TOKEN, tmp_path / "anywhere", home=tmp_path).steps
    assert isinstance(step, s.AppendFile)
    assert step.path == tmp_path / ".hermes" / "config.yaml"
    assert f"Authorization: Bearer {TOKEN}" in step.content
    s.apply(step)
    assert f"Authorization: Bearer {TOKEN}" in step.path.read_text()


def test_not_logged_in_is_a_one_line_error(tmp_path, monkeypatch) -> None:
    # The connect guide (docs/connect-your-agent.md) has agents run `relay whoami` to check this.
    from relayagents.cli import client

    monkeypatch.delenv("RELAY_URL", raising=False)
    monkeypatch.delenv("RELAY_TOKEN", raising=False)
    monkeypatch.setattr(client, "CREDENTIALS", tmp_path / "missing.json")
    result = CliRunner().invoke(main.app, ["whoami"])
    assert result.exit_code == 1
    assert "not logged in: run `relay login`" in result.output
    assert isinstance(result.exception, SystemExit)  # not an uncaught error with a traceback


# ---- --env-file: the token goes into a file, never into the output -----------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_set_env_line_replaces_only_its_own_line(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text(
        "alias ll='ls -l'\n"
        "export RELAY_CODEX_TOKEN=rly_old\n"
        "RELAY_CODEX_TOKEN='rly_older'\n"
        "if true; then\n  export RELAY_CODEX_TOKEN=x\nfi\n"  # inside a block: removing breaks it
        "export RELAY_CODEX_TOKEN=y; export X=1\n"  # compound: keep the rest of the line
        "export PATH=$PATH:/opt"
    )
    rc.chmod(0o644)
    s.set_env_line(rc, "RELAY_CODEX_TOKEN", TOKEN)
    assert rc.read_text() == (
        "alias ll='ls -l'\n"
        "if true; then\n  export RELAY_CODEX_TOKEN=x\nfi\n"
        "export RELAY_CODEX_TOKEN=y; export X=1\n"
        f"export PATH=$PATH:/opt\nexport RELAY_CODEX_TOKEN={TOKEN}\n"
    )
    assert rc.stat().st_mode & 0o777 == 0o600  # it holds a token now
    assert [f.name for f in tmp_path.iterdir()] == [".zshrc"]  # no temp file left behind


def test_set_env_line_keeps_crlf_and_refuses_binary(tmp_path: Path) -> None:
    rc = tmp_path / "profile"
    rc.write_bytes(b"export A=1\r\nexport RELAY_CODEX_TOKEN=old\r\n")
    s.set_env_line(rc, "RELAY_CODEX_TOKEN", TOKEN)
    assert rc.read_bytes() == f"export A=1\r\nexport RELAY_CODEX_TOKEN={TOKEN}\r\n".encode()
    rc.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(s.SetupError, match="not UTF-8"):
        s.set_env_line(rc, "RELAY_CODEX_TOKEN", TOKEN)


def test_set_env_line_creates_a_private_file_and_keeps_symlinks(tmp_path: Path) -> None:
    new = tmp_path / "proj" / ".envrc"
    s.set_env_line(new, "RELAY_OPENCODE_TOKEN", TOKEN)
    assert new.stat().st_mode & 0o777 == 0o600
    dotfiles = tmp_path / "dotfiles" / "zshrc"
    dotfiles.parent.mkdir()
    dotfiles.write_text("# mine\n")
    link = tmp_path / ".zshrc"
    link.symlink_to(dotfiles)
    s.set_env_line(link, "RELAY_CODEX_TOKEN", TOKEN)
    assert link.is_symlink() and TOKEN in dotfiles.read_text()


def test_env_file_git_would_track_is_refused(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    with pytest.raises(s.SetupError, match="not ignored"):
        s.check_env_file(tmp_path / ".envrc")
    (tmp_path / ".git" / "info" / "exclude").write_text(".envrc\n")
    s.check_env_file(tmp_path / ".envrc")  # ignored: fine
    with pytest.raises(s.SetupError, match="not ignored"):
        s.check_env_file(tmp_path / "config" / "new" / "token.sh")  # folders that do not exist yet
    outside = tmp_path.parent / f"{tmp_path.name}-home" / ".zshrc"
    outside.parent.mkdir()
    s.check_env_file(outside)  # not in any work tree: fine


def test_env_file_check_fails_closed_when_git_cannot_answer(tmp_path, monkeypatch) -> None:
    def git_refuses(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 128, "", "fatal: detected dubious ownership")

    monkeypatch.setattr(s.subprocess, "run", git_refuses)
    with pytest.raises(s.SetupError, match="dubious ownership"):
        s.check_env_file(tmp_path / ".envrc")


def test_env_file_shared_with_others_is_announced_as_made_private(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("# mine\n")
    (home / ".zshrc").chmod(0o644)
    plan = s.plan("codex", URL, TOKEN, tmp_path / "proj", home=home, env_file=home / ".zshrc")
    assert "now private to you" in s.describe(plan.steps[-1], reveal=False)


def test_env_file_in_a_repo_is_refused_without_git(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(s.shutil, "which", lambda name: None)
    with pytest.raises(s.SetupError, match="git is not on PATH"):
        s.check_env_file(tmp_path / "sub" / ".envrc")
    s.check_env_file(tmp_path.parent / f"{tmp_path.name}-elsewhere" / ".envrc")


def test_env_file_check_ignores_inherited_git_env(tmp_path, monkeypatch) -> None:
    # GIT_DIR pointing at some other repo must not make the project's .envrc look unversioned.
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    (other / ".git" / "info" / "exclude").write_text(".envrc\n")  # ignored there, not here
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    with pytest.raises(s.SetupError, match="not ignored"):
        s.check_env_file(project / ".envrc")


def test_env_file_symlinked_into_a_dotfiles_repo_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "dotfiles"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "zshrc").write_text("# tracked\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").symlink_to(repo / "zshrc")
    with pytest.raises(s.SetupError, match="not ignored"):
        s.plan("codex", URL, TOKEN, tmp_path / "proj", home=home, env_file=home / ".zshrc")


def test_env_file_plan_never_shows_the_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = s.plan("cursor", URL, TOKEN, tmp_path / "proj", home=home, env_file=home / ".zshrc")
    (set_env,) = [st for st in plan.steps if isinstance(st, s.SetEnv)]
    assert set_env == s.SetEnv(home / ".zshrc", "RELAY_CURSOR_TOKEN", TOKEN)
    assert all(TOKEN not in s.describe(st, reveal=True) for st in plan.steps)


def test_env_file_is_only_for_env_token_agents(tmp_path: Path) -> None:
    with pytest.raises(s.SetupError, match="--env-file applies only to"):
        s.plan("claude-code", URL, TOKEN, tmp_path, env_file=tmp_path / ".envrc")


def test_setup_agent_env_file_keeps_the_token_out_of_the_output(tmp_path, monkeypatch) -> None:
    fake = _FakeClient()
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    (project / ".git" / "info" / "exclude").write_text(".envrc\n")
    monkeypatch.setattr(main, "_client", lambda: fake)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    envrc = project / ".envrc"
    args = ["setup-agent", "codex", "--project", str(project), "--env-file", str(envrc)]
    result = CliRunner().invoke(main.app, [*args, "--write"])
    assert result.exit_code == 0, result.output
    assert TOKEN not in result.output
    assert envrc.read_text() == f"export RELAY_CODEX_TOKEN={TOKEN}\n"
