# Connect a project to Relay (instructions for the coding agent)

Your human pointed you here and asked you to connect a project to Relay. Do the steps below
yourself, and stop only where a step says the human has to act. When you're done, the project's
sessions have Relay's tools and the `relay` skill. Their other projects are not affected
([ADR-0010](adr/0010-coding-agents-opt-in-per-project.md)).

Everything an agent sends to Relay is visible to the whole team, so this is opt-in per project.
The folder you connect is the one the human asked about, normally the one this session is in. Do
not connect the home folder, a personal project, or several projects at once on your own
initiative. If you are not sure the folder is team work, ask first.

Several commands below need the network, and some write outside the project
(`~/.agents/skills`, a shell profile). If your sandbox blocks them, ask for approval and say
why. Run every command from the project root.

## 1. Know which agent you are

| You are | Argument for `setup-agent` | Token variable |
|---|---|---|
| Claude Code | `claude-code` | none (stored in Claude Code's local config) |
| Codex | `codex` | `RELAY_CODEX_TOKEN` |
| OpenCode | `opencode` | `RELAY_OPENCODE_TOKEN` |
| Cursor | `cursor` | `RELAY_CURSOR_TOKEN` |

If you are another agent, stop and tell the human that per-project setup does not support it yet.
Do not use `generic`: it puts the human's own token in the config.

## 2. Install the `relay` CLI

Check with `relay setup-agent --help`. If `relay` is missing, or the help does not list your
agent and `--env-file`, install or upgrade it:

```bash
uv tool install --upgrade git+https://github.com/relayagents/relay
```

If `uv` is missing, install it first with the official installer
(<https://docs.astral.sh/uv/getting-started/installation/>), or ask the human. If `relay` is still
not found afterwards, uv's tool folder (usually `~/.local/bin`) is not on the PATH. `uv tool
update-shell` fixes that for new terminals, including the human's.

## 3. Make sure the CLI is logged in as the human

Run `relay whoami`. If the output shows `"kind": "human"` under `actor`, go to step 4.

- **If it shows `"kind": "agent"`:** `RELAY_TOKEN` is set in the environment and holds an agent
  token. Setup needs the human's login. Tell the human to remove that export (from the shell
  profile, or wherever it is set) and start a new session.
- **If it says `not logged in`:** do not set `RELAY_URL` or `RELAY_TOKEN` yourself, even though
  the error mentions them. Ask the human, in one message:
  - If your admin gave you a login token, run this in a terminal of your own and tell me when it
    says `logged in as ...`. Don't paste the token here, because it is your own human token:
    `relay login --url <Relay URL> --token <token>`.
  - Otherwise, send me the Relay URL and your Relay user id (your admin gave you both), and I'll
    log you in through Slack.

  If `RELAY_URL` is already set, put its value in the command for them.

  **If they have no token**, log them in through Slack:

  ```bash
  relay login --url <RELAY_URL> --user <user id>
  ```

  It prints a line, then waits up to 10 minutes for approval, so run it in the background or with
  a long timeout:
  - `Check your Slack DM from Relay and approve login code ABCD12`: tell the human to approve
    that code in their Slack DM from Relay.
  - `Slack is not configured. Ask an admin to run on the node: relay admin approve-login ...`:
    pass that command to the human for their Relay admin, who runs it on the Relay server.

  Wait for `logged in as <user>`. If the human switches to the token route instead, stop the
  waiting command; the unused request expires on its own.

After any login, run `relay whoami` again and check for `"kind": "human"` before going on.

## 4. Connect the project

`setup-agent` mints an agent token for this agent, project, and machine. It revokes the one it
replaces, so rerunning it is safe, and it refuses the home folder. `Revoked 1 earlier token(s)
for this project.` means this project was connected before; that is expected. You may run it
because your human asked you to connect the project. Setup (`whoami`, `login`, `setup-agent`) is
the one time you run `relay` commands yourself. At any other time the `relay` skill tells you not
to, because the CLI acts as the human.

**Claude Code:**

```bash
relay setup-agent claude-code --write
```

This installs and enables the Relay plugin, which carries the skill, and adds the MCP server.
Both are scoped to this folder and to this human. Go to step 5.

**Codex, OpenCode, Cursor:** the project's MCP config reads the token from the variable in
step 1, so the config file holds no secret. Choose the file that sets that variable, then let
`setup-agent` write it there with `--env-file`. That way the token never appears in your output.

- **If `direnv` is set up** (`command -v direnv` finds it, and
  `grep -l 'direnv hook' ~/.zshrc ~/.bashrc ~/.bash_profile ~/.config/fish/config.fish` finds a
  file): use `.envrc` in the project root. Make sure git ignores it first, without touching the
  team's `.gitignore`:

  ```bash
  x="$(git rev-parse --git-path info/exclude)"; mkdir -p "$(dirname "$x")"
  git check-ignore -q .envrc || printf '\n.envrc\n' >> "$x"
  relay setup-agent <your argument> --write --env-file .envrc
  direnv allow
  ```

- **Otherwise** use the shell profile for `$SHELL`. For zsh that is `~/.zshrc`. For bash on Linux
  it is `~/.bashrc`, and for bash on macOS `~/.bash_profile`, because Terminal starts login
  shells. For any other shell, ask the human which file to use. A profile that does not exist yet
  is created, readable only by the human.

  ```bash
  relay setup-agent <your argument> --write --env-file ~/.zshrc
  ```

  The profile holds one variable per agent for all projects. Each setup replaces the line with
  a new token and revokes the one it overwrote. All projects read that one line, so projects
  connected earlier keep working once their next session starts.

`--env-file` replaces an earlier line for the same variable and leaves the rest of the file
alone. It refuses a file that git would track, such as a profile symlinked from a dotfiles repo.
If that happens, ask the human where the variable should go. It cannot see a bare-repo dotfiles
setup (`git --git-dir=~/.dotfiles`). If `~/.dotfiles` or a similar folder exists, ask the human
before writing to a profile. A file that other users could read is made private to the human,
and the output says so; pass that on. Do not print the file or the token.

Besides the token, the command writes the project's MCP config (`.codex/config.toml`,
`opencode.json`, or `.cursor/mcp.json`) and installs the skill into `~/.agents/skills/relay`.
Do not commit that config file. Committing it opts the project in for every teammate who uses
that agent. That is the team's call, so mention it to the human and leave the file uncommitted.

## 5. Hand back to the human

The new tools load in a new session, not this one. Tell the human what you did and what is left,
briefly:

- **Claude Code:** start a new session in this project (or run `/reload-plugins` and then `/mcp`).
  You can confirm it first with `claude mcp list` in the project: look for `relay ... Connected`.
- **Codex:** open a new terminal so it picks up the token (direnv loads `.envrc` on `cd` into the
  project), and start `codex` in the project from there. Codex reads `.codex/config.toml` only in
  trusted projects. If it asks whether to trust the folder, they should answer yes. If they
  declined before, Codex will not ask again. Trusting it then means adding this to
  `~/.codex/config.toml`:

  ```toml
  [projects."<absolute project root>"]
  trust_level = "trusted"
  ```

  Trust is the human's decision. Make that change only if they ask you to. Once the folder is
  trusted and the variable is set, `codex mcp list` in the project lists `relay`.
- **OpenCode:** open a new terminal and start `opencode` in the project from there.
- **Cursor:** quit Cursor and launch it from a new terminal in the project (`cursor .`). Opened
  from the Dock or Start menu, it does not see shell variables.

In the new session, the Relay tools (`my_items`, `recall`, `report`, ...) are present and the
`relay` skill applies.

## Troubleshooting

- **`not logged in`:** go back to step 3.
- **`... is your home folder or the filesystem root`:** `cd` into the project first.
- **`... is in a git work tree and not ignored`:** the `--env-file` target could be committed.
  Ignore it as in step 4, or pick another file.
- **`... exists and is not Relay's skill`:** something else is installed at
  `~/.agents/skills/relay`. Ask the human before moving it.
- **The tools are still missing in a new session:** for Codex, OpenCode, or Cursor, check
  without printing it (`test -n "$RELAY_CODEX_TOKEN"`, and so on) that the variable is set in
  the environment the agent started from, and that Codex trusts the project. Otherwise run
  step 4 again.
