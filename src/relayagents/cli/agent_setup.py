"""`relay setup-agent`: connect one coding agent to Relay in one project, and nothing more.

Relay is opt-in per project (ADR-0010). A plan is a list of steps, built without side effects so
that the printed form and the applied form cannot drift and tests can check either one:

- Claude Code: install and enable the Relay plugin (it carries the skill) at local scope, and add
  the MCP server at local scope. Both are private to this user and this folder.
- Codex, OpenCode, and Cursor: write the project's own MCP config with the token read from an
  environment variable, so the file holds no secret, and install the skill once at
  ``~/.agents/skills``. The skill does nothing where the Relay tools are not connected. With
  ``--env-file``, the token goes straight into that file (refused if git would track it), so an
  agent running setup for its human never has the token in its output.
- Hermes and generic: unchanged; Hermes runs in the team's container, where Relay is always on.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

AGENTS = ("claude-code", "codex", "opencode", "cursor", "hermes", "generic")
# Laptop coding agents: connected per project, never from the home folder (ADR-0010).
PROJECT_AGENTS = ("claude-code", "codex", "opencode", "cursor")
MARKETPLACE_SOURCE = "relayagents/relay"
MARKETPLACE_SPARSE = (".claude-plugin", "src/relayagents/agent_plugin")
PLUGIN_ID = "relay@relayagents"
# Not RELAY_TOKEN: the `relay` CLI prefers RELAY_TOKEN over its stored login, so exporting an agent
# token under that name would make the human's own CLI act as the agent.
TOKEN_ENV = {
    "codex": "RELAY_CODEX_TOKEN",
    "opencode": "RELAY_OPENCODE_TOKEN",
    "cursor": "RELAY_CURSOR_TOKEN",
}
SKILL_NAME = "relay"


class SetupError(Exception):
    """A step cannot be planned or applied safely; the message says what to do instead."""


@dataclass(frozen=True)
class Run:
    argv: list[str]
    cwd: Path
    secret: str | None = None  # masked when the step is described after it has been applied
    allow_failure: bool = False
    already_done: str | None = None  # a failure whose output says this means "nothing to do"


@dataclass(frozen=True)
class WriteFile:
    path: Path
    content: str


@dataclass(frozen=True)
class AppendFile:
    path: Path
    content: str


@dataclass(frozen=True)
class InstallSkill:
    dest: Path


@dataclass(frozen=True)
class SetEnv:
    """Set ``export name=value`` in a shell file, replacing an earlier line for the same name."""

    path: Path
    name: str
    value: str  # a token: never described, never printed


@dataclass(frozen=True)
class Note:
    text: str


Step = Run | WriteFile | AppendFile | InstallSkill | SetEnv | Note


@dataclass
class Plan:
    agent: str
    project: Path
    steps: list[Step] = field(default_factory=list)


def skill_source() -> Traversable:
    """The one SKILL.md (and anything beside it), shared by the Claude Code plugin and the CLI."""
    return resources.files("relayagents") / "agent_plugin" / "skills" / SKILL_NAME


def project_root(start: Path) -> Path:
    """The git work tree containing ``start``, or ``start`` itself outside a repository."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return start.resolve()
    return Path(out.stdout.strip()).resolve() if out.returncode == 0 else start.resolve()


def generic_mcp_json(url: str, token: str) -> str:
    return json.dumps(
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
    )


def token_label(agent: str, project: Path, env_file: Path | None = None) -> str:
    """One label per place the token is kept, so a rerun can revoke exactly what it replaces.

    Normally that is the agent, project, and machine: the same agent in another project, or on
    another machine, keeps its own token. A token kept in a file outside the project (a shell
    profile) is shared by every project that reads it, so it is labelled by that file instead,
    and the next setup that writes there revokes the token it overwrites.
    """
    where = project.resolve()
    if env_file is not None:
        real = env_file.expanduser().resolve()
        if not real.is_relative_to(where):
            where = real
    key = hashlib.sha256(f"{socket.gethostname()}:{where}".encode()).hexdigest()[:10]
    name = re.sub(r"[^\w.-]", "-", where.name)[:20] or "root"
    return f"agent:{agent}:{name}:{key}"[:64]


# ---- planning -----------------------------------------------------------------------------------


def plan(
    agent: str,
    url: str,
    token: str,
    project: Path,
    *,
    marketplace: str = MARKETPLACE_SOURCE,
    home: Path | None = None,
    env_file: Path | None = None,
) -> Plan:
    if agent not in AGENTS:
        raise SetupError(f"unknown agent {agent!r}; choose from {', '.join(AGENTS)}")
    if env_file is not None:
        if agent not in TOKEN_ENV:
            raise SetupError(f"--env-file applies only to {', '.join(TOKEN_ENV)}")
        check_env_file(env_file)
    home = home or Path.home()
    if agent in PROJECT_AGENTS and project.resolve() in (
        home.resolve(),
        Path(project.resolve().anchor),
    ):
        # From ~, Codex's .codex/config.toml and Cursor's .cursor/mcp.json are their user-wide
        # configs: writing there would turn Relay on in every project.
        raise SetupError(
            f"{project} is your home folder or the filesystem root, not a project;"
            " run setup-agent from the project that should use Relay"
        )
    p = Plan(agent=agent, project=project)
    if agent == "claude-code":
        p.steps = _plan_claude_code(url, token, project, marketplace)
    elif agent == "codex":
        p.steps = _plan_codex(url, token, project, home, env_file)
    elif agent == "opencode":
        p.steps = _plan_opencode(url, token, project, home, env_file)
    elif agent == "cursor":
        p.steps = _plan_cursor(url, token, project, home, env_file)
    elif agent == "hermes":
        p.steps = [
            AppendFile(
                home / ".hermes" / "config.yaml",
                f"mcp_servers:\n  relay:\n    url: {url}/mcp\n    headers:\n      Authorization: Bearer {token}\n",
            )
        ]
    else:
        p.steps = [Note(f"Add this to your agent's MCP config:\n{generic_mcp_json(url, token)}")]
    return p


def _plan_claude_code(url: str, token: str, project: Path, marketplace: str) -> list[Step]:
    add_marketplace = ["claude", "plugin", "marketplace", "add", marketplace]
    if not Path(marketplace).expanduser().exists():  # a git source: fetch only the plugin
        add_marketplace += ["--sparse", *MARKETPLACE_SPARSE]
    return [
        Run(add_marketplace, project),
        Run(["claude", "plugin", "install", PLUGIN_ID, "--scope", "local"], project),
        # The plugin ships disabled (defaultEnabled: false), so a local install alone leaves it off.
        Run(
            ["claude", "plugin", "enable", PLUGIN_ID, "--scope", "local"],
            project,
            already_done="already enabled",
        ),
        # `mcp add` refuses an existing name; removing first makes a rerun rotate the token.
        Run(["claude", "mcp", "remove", "relay", "--scope", "local"], project, allow_failure=True),
        Run(
            [
                "claude",
                "mcp",
                "add",
                "--scope",
                "local",
                "--transport",
                "http",
                "relay",
                f"{url}/mcp",
                "--header",
                f"Authorization: Bearer {token}",
            ],
            project,
            secret=token,
        ),
        Note(
            f"Relay is on for Claude Code in {project} only. Start a new session there"
            " (or run /reload-plugins and /mcp) to load it."
        ),
    ]


def _plan_codex(
    url: str, token: str, project: Path, home: Path, env_file: Path | None
) -> list[Step]:
    env = TOKEN_ENV["codex"]
    config = project / ".codex" / "config.toml"
    existing = config.read_text() if config.exists() else ""
    steps: list[Step] = [
        WriteFile(config, merge_codex_config(existing, url, env)),
        InstallSkill(check_skill_dest(home / ".agents" / "skills" / SKILL_NAME)),
    ]
    global_config = home / ".codex" / "config.toml"
    if global_config.exists() and _codex_has_relay(global_config.read_text()):
        steps.append(
            Note(
                f"{global_config} also defines [mcp_servers.relay], which connects Relay in every"
                " project. Remove that table so only opted-in projects connect."
            )
        )
    set_env, note = _token_steps(env, token, "Codex", project, env_file, trust=True)
    steps += [*set_env, Note(note)]
    return steps


def _plan_opencode(
    url: str, token: str, project: Path, home: Path, env_file: Path | None
) -> list[Step]:
    env = TOKEN_ENV["opencode"]
    config = project / "opencode.json"
    existing = config.read_text() if config.exists() else ""
    steps: list[Step] = [
        WriteFile(config, merge_opencode_config(existing, url, env)),
        InstallSkill(check_skill_dest(home / ".agents" / "skills" / SKILL_NAME)),
    ]
    global_config = home / ".config" / "opencode" / "opencode.json"
    if global_config.exists() and '"relay"' in global_config.read_text():
        steps.append(
            Note(
                f"{global_config} may also define a relay MCP server, which connects Relay in every"
                " project. Remove it there so only opted-in projects connect."
            )
        )
    set_env, note = _token_steps(env, token, "OpenCode", project, env_file, trust=False)
    steps += [*set_env, Note(note)]
    return steps


def _plan_cursor(
    url: str, token: str, project: Path, home: Path, env_file: Path | None
) -> list[Step]:
    env = TOKEN_ENV["cursor"]
    config = project / ".cursor" / "mcp.json"
    existing = config.read_text() if config.exists() else ""
    steps: list[Step] = [
        WriteFile(config, merge_cursor_config(existing, url, env)),
        # Cursor also reads ~/.agents/skills, so one install serves Codex, OpenCode, and Cursor.
        InstallSkill(check_skill_dest(home / ".agents" / "skills" / SKILL_NAME)),
    ]
    global_config = home / ".cursor" / "mcp.json"
    if global_config.exists() and '"relay"' in global_config.read_text():
        steps.append(
            Note(
                f"{global_config} may also define a relay MCP server, which connects Relay in every"
                " project. Remove it there so only opted-in projects connect."
            )
        )
    set_env, note = _token_steps(env, token, "Cursor", project, env_file, trust=False)
    steps += [
        *set_env,
        Note(
            note + "\nCursor reads it from its own environment: if you open Cursor from the"
            "\nDock, set it where apps see it too, or launch Cursor from that shell."
        ),
    ]
    return steps


def _token_steps(
    env: str, token: str, harness: str, project: Path, env_file: Path | None, *, trust: bool
) -> tuple[list[SetEnv], str]:
    """Where the agent token goes: into ``env_file`` (never printed), or a line to copy."""
    if env_file is not None:
        steps = [SetEnv(env_file, env, token)]
        lines = [
            f"{harness} reads the agent token from ${env}, now set in {env_file}.",
            f"Start {harness} from a shell that has loaded it (a new terminal).",
        ]
        real = env_file.expanduser().resolve()
        if real.is_file() and real.stat().st_mode & 0o077:
            lines.append(f"{env_file} is now private to you (group and other access removed).")
    else:
        steps = []
        lines = [
            f"{harness} reads the agent token from ${env}. Set it where {harness} starts, for"
            " example in",
            "your shell profile or a gitignored .envrc in this project (direnv), or rerun with"
            " --env-file:",
            f"  export {env}={token}",
        ]
    lines += [
        f"The config file holds no secret, so committing it opts {project.name} in for teammates too;",
        "each of them runs `relay setup-agent` with their own token.",
    ]
    if trust:
        lines.append("Codex loads a project's .codex/config.toml only when you trust the project.")
    return steps, "\n".join(lines)


def check_env_file(path: Path) -> None:
    """Refuse a file git would track: a token must never reach a commit (the project, dotfiles).

    Follows symlinks (a stow-managed ~/.zshrc lives in a repo) and fails closed whenever git
    cannot answer. A bare-repo dotfiles setup (``--git-dir=~/.dotfiles``) is invisible to it.
    """
    real = path.expanduser().resolve()
    if real.is_dir():
        raise SetupError(f"{path} is a folder; --env-file takes a file such as .envrc or ~/.zshrc")
    anchor = real.parent
    while not anchor.is_dir():  # the file may go into a folder that does not exist yet
        anchor = anchor.parent
    git = shutil.which("git")
    if git is None:
        # No git to ask, but a GUI client or a later install could still commit it.
        if any((d / ".git").exists() for d in (anchor, *anchor.parents)):
            raise SetupError(
                f"{real} is inside a git repository and git is not on PATH to check it"
            )
        return
    # Ask about this file's own repository, in a fixed language, across mount points.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"LC_ALL": "C", "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1"}

    def ask(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [git, "-C", str(anchor), *args], capture_output=True, text=True, check=False, env=env
        )

    inside = ask("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0:
        if inside.stderr.startswith("fatal: not a git repository"):
            return
        raise SetupError(
            f"git could not tell whether {real} would be committed"
            f" ({inside.stderr.strip() or 'no output'}); choose a file outside any repository"
        )
    if inside.stdout.strip() != "true":
        return  # inside a .git folder or a bare repository: not a work tree
    ignored = ask("check-ignore", "-q", str(real))
    if ignored.returncode == 1:
        raise SetupError(
            f"{real} is in a git work tree and not ignored, so the token could be committed;"
            " add it to .gitignore or .git/info/exclude first, or choose another file"
        )
    if ignored.returncode != 0:
        raise SetupError(f"git could not check {real} ({ignored.stderr.strip() or 'no output'})")


# ---- config merges ------------------------------------------------------------------------------

_TABLE_HEADER = re.compile(r"^\s*\[")
_RELAY_TABLE = re.compile(r"^\s*\[\s*mcp_servers\s*\.\s*(\"relay\"|relay)\s*(\..*)?\]\s*(#.*)?$")


def _codex_has_relay(text: str) -> bool:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return bool(re.search(r"mcp_servers\s*\.\s*\"?relay", text))
    return "relay" in data.get("mcp_servers", {})


def merge_codex_config(existing: str, url: str, env: str) -> str:
    """Replace (or add) ``[mcp_servers.relay]`` in a Codex config, leaving everything else as is."""
    try:
        tomllib.loads(existing)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f".codex/config.toml is not valid TOML ({exc}); fix it first") from exc
    kept: list[str] = []
    skipping = False
    for line in existing.splitlines(keepends=True):
        if _TABLE_HEADER.match(line):
            skipping = bool(_RELAY_TABLE.match(line))
        if not skipping:
            kept.append(line)
    body = "".join(kept).rstrip("\n")
    if _codex_has_relay(body):  # defined some other way (dotted keys, inline table): don't guess
        raise SetupError(
            "the relay MCP server in .codex/config.toml is not a [mcp_servers.relay] table;"
            " remove it and run setup-agent again"
        )
    block = (
        "[mcp_servers.relay]\n"
        f"url = {json.dumps(f'{url}/mcp')}\n"
        f"bearer_token_env_var = {json.dumps(env)}\n"
    )
    merged = f"{body}\n\n{block}" if body else block
    relay = tomllib.loads(merged)["mcp_servers"]["relay"]
    if relay != {"url": f"{url}/mcp", "bearer_token_env_var": env}:
        raise SetupError("could not update .codex/config.toml safely; edit it by hand")
    return merged


def _load_json_object(existing: str, name: str) -> dict[str, Any] | None:
    """Parse a JSON config we are about to merge into; ``None`` when the file is new or empty."""
    if not existing.strip():
        return None
    try:
        data = json.loads(existing)
    except json.JSONDecodeError as exc:
        raise SetupError(
            f"{name} is not plain JSON (comments?); add the relay server by hand"
        ) from exc
    if not isinstance(data, dict):
        raise SetupError(f"{name} is not a JSON object")
    return data


def _json_table(data: dict[str, Any], key: str, name: str) -> dict[str, Any]:
    table = data.setdefault(key, {})
    if not isinstance(table, dict):
        raise SetupError(f'{name} has a "{key}" key that is not an object')
    return table


def merge_opencode_config(existing: str, url: str, env: str) -> str:
    """Set ``mcp.relay`` in an OpenCode config; the token comes from ``{env:...}``, never the file."""
    data = _load_json_object(existing, "opencode.json")
    if data is None:
        data = {"$schema": "https://opencode.ai/config.json"}
    _json_table(data, "mcp", "opencode.json")["relay"] = {
        "type": "remote",
        "url": f"{url}/mcp",
        "enabled": True,
        "headers": {"Authorization": f"Bearer {{env:{env}}}"},
    }
    return json.dumps(data, indent=2) + "\n"


def merge_cursor_config(existing: str, url: str, env: str) -> str:
    """Set ``mcpServers.relay`` in ``.cursor/mcp.json``; the token comes from ``${env:...}``."""
    data = _load_json_object(existing, ".cursor/mcp.json") or {}
    _json_table(data, "mcpServers", ".cursor/mcp.json")["relay"] = {
        "url": f"{url}/mcp",
        "headers": {"Authorization": f"Bearer ${{env:{env}}}"},
    }
    return json.dumps(data, indent=2) + "\n"


# ---- describing and applying --------------------------------------------------------------------


def describe(step: Step, *, reveal: bool) -> str:
    """Human-readable form. ``reveal=False`` masks the token in commands that are about to run."""
    if isinstance(step, Run):
        args = [
            a.replace(step.secret, "***") if step.secret and not reveal else a for a in step.argv
        ]
        return "$ " + " ".join(_quote(a) for a in args)
    if isinstance(step, WriteFile):
        return f"# write {step.path}\n{step.content}"
    if isinstance(step, AppendFile):
        return f"# append to {step.path}\n{step.content}"
    if isinstance(step, InstallSkill):
        return f"# install the relay skill into {step.dest}"
    if isinstance(step, SetEnv):
        return f"# set {step.name} in {step.path} (the token is not shown)"
    return step.text


def _quote(arg: str) -> str:
    return arg if re.fullmatch(r"[\w@%+=:,./-]+", arg) else json.dumps(arg)


Runner = Callable[..., subprocess.CompletedProcess[str]]


def apply(step: Step, *, runner: Runner | None = None) -> None:
    if isinstance(step, Run):
        run = runner or subprocess.run
        try:
            done = run(step.argv, cwd=step.cwd, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise SetupError(f"`{step.argv[0]}` is not on your PATH") from exc
        detail = (done.stderr or done.stdout or "").strip()
        if step.already_done and step.already_done in detail:
            return
        if done.returncode != 0 and not step.allow_failure:
            if step.secret:
                detail = detail.replace(step.secret, "***")
            raise SetupError(f"`{' '.join(step.argv[:3])}` failed: {detail}")
    elif isinstance(step, WriteFile):
        step.path.parent.mkdir(parents=True, exist_ok=True)
        step.path.write_text(step.content)
    elif isinstance(step, AppendFile):
        step.path.parent.mkdir(parents=True, exist_ok=True)
        with step.path.open("a") as f:
            f.write("\n" + step.content)
    elif isinstance(step, InstallSkill):
        install_skill(step.dest)
    elif isinstance(step, SetEnv):
        check_env_file(step.path)  # again: a symlink or ignore rule may have changed since planning
        set_env_line(step.path, step.name, step.value)


def set_env_line(path: Path, name: str, value: str) -> None:
    """Replace (or add) ``export name=value`` in a shell file without disturbing the rest of it.

    Only a top-level line that is nothing but the assignment is replaced; an indented or compound
    line is left alone, and the new line, appended last, wins. The write is atomic, keeps the file's
    line endings, drops group and other permissions (the file now holds a token), and goes to the
    symlink's target so the link survives.
    """
    real = path.expanduser().resolve()
    try:
        with open(real, encoding="utf-8", newline="") as f:
            old = f.read()
        mode = real.stat().st_mode & 0o700  # it now holds a token: private to its owner
    except FileNotFoundError:
        old, mode = "", 0o600
    except UnicodeDecodeError as exc:
        raise SetupError(f"{path} is not UTF-8 text; choose another file") from exc
    eol = "\r\n" if "\r\n" in old else "\n"
    lone = re.compile(rf"^(export[ \t]+)?{re.escape(name)}=[^;&|`$\\\s]*[ \t]*\r?\n?$")
    body = "".join(line for line in old.splitlines(keepends=True) if not lone.match(line))
    if body and not body.endswith("\n"):
        body += eol
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{real.name}.", dir=real.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(f"{body}export {name}={shlex.quote(value)}{eol}")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, real)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def check_skill_dest(dest: Path) -> Path:
    """Refuse, before anything is written, a skill folder that is not an earlier copy of ours."""
    if dest.is_symlink():
        raise SetupError(f"{dest} is a symlink; remove it or install the skill there by hand")
    existing = dest / "SKILL.md"
    ours = existing.is_file() and _skill_name(existing.read_text()) == SKILL_NAME
    if dest.exists() and not ours:
        raise SetupError(f"{dest} exists and is not Relay's skill; move it aside first")
    return dest


def install_skill(dest: Path, source: Traversable | None = None) -> None:
    """Copy the packaged skill to ``dest``, replacing only an earlier copy of this same skill."""
    source = source or skill_source()
    check_skill_dest(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}.", dir=dest.parent))
    backup = dest.parent / f".{SKILL_NAME}.previous"
    try:
        _copy_tree(source, staging)
        if backup.exists():
            shutil.rmtree(backup)
        if dest.exists():
            os.replace(dest, backup)  # keep the old copy until the new one is in place
        try:
            os.replace(staging, dest)
        except OSError:
            if backup.exists() and not dest.exists():
                os.replace(backup, dest)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _copy_tree(src: Traversable, dst: Path) -> None:
    for child in src.iterdir():
        if child.name == "__pycache__":
            continue
        if child.is_dir():
            (dst / child.name).mkdir()
            _copy_tree(child, dst / child.name)
        else:
            (dst / child.name).write_bytes(child.read_bytes())


def _skill_name(text: str) -> str | None:
    m = re.match(r"---\s*\n(.*?)\n---", text, flags=re.S)
    if not m:
        return None
    name = re.search(r"^name:\s*(\S+)\s*$", m.group(1), flags=re.M)
    return name.group(1) if name else None
