# ADR-0010: Coding agents get Relay per project, through one skill shipped from this repo

**Status:** accepted · 2026-10-08

## Context

Relay reaches a teammate through the coding agent they already use on their laptop. Two things are
needed: the connection (Relay's MCP tools under the agent's own token) and the behavior (when to
`report`, when to `request_approval`, what not to send). Before this ADR, `relay setup-agent`
wrote the connection only, and the behavior existed as a short MCP `instructions` string and a
Hermes-only skill baked into the Hermes image.

A laptop agent works in many folders, and most of them are not team work: a personal site, a
course assignment, someone else's open-source project. Anything the agent sends to Relay is visible
to the whole team, so Relay must be off by default and on only where a project opted in. A lab
(the first being the Agentic Learning AI Lab, whose `lab-core` plugin every member installs) wants
its members to get Relay with little effort, but that plugin is installed for every project, so it
cannot be what switches Relay on.

What the harnesses support, checked against their docs in October 2026:

| Harness | Project-scoped MCP | Token kept out of the file | Skills |
|---|---|---|---|
| Claude Code | `claude mcp add --scope local` (private, keyed to the folder); plugins enable per project | yes | via plugin, enabled per project |
| Codex | `.codex/config.toml` in trusted projects | `bearer_token_env_var` | `.agents/skills` in the repo, or `~/.agents/skills` |
| OpenCode | `opencode.json` at the project root | `{env:NAME}` | `.agents/skills`, `~/.agents/skills`, and others |
| Hermes | no; `~/.hermes/config.yaml` only | n/a | `skills.external_dirs` |

A behavior test settled what the skill must say. Given only today's MCP instructions, agents
already reported finished work, cited event ids, and asked for approval on work that came from an
action item. They failed in two places: they sent an approval to Slack for a PR the human had just
asked for in the session, and, with the Relay tools absent but the `relay` CLI logged in, they
published a report through the CLI under the human's token. With the skill, both were fixed and
nothing regressed.

## Decision

1. **One skill, here.** `src/relayagents/agent_plugin/skills/relay/SKILL.md` is the only copy. It
   lives inside the Python package, so the `relay` CLI can install it from the wheel, and it is
   also a Claude Code plugin (`relay`, in this repo's marketplace `relayagents`). `tests/test_docs.py`
   fails when the skill's tool table or approval keys drift from the registry and policy.
2. **Opt-in is per project, and `relay setup-agent <agent> --write` is the switch.** Run inside a
   project, it connects that agent there only:
   - Claude Code: install and enable the plugin at local scope and add the MCP server at local
     scope. The plugin ships with `defaultEnabled: false`, so a user-wide install by mistake stays
     off.
   - Codex and OpenCode: write the project's MCP config with the token read from
     `RELAY_CODEX_TOKEN` / `RELAY_OPENCODE_TOKEN` (not `RELAY_TOKEN`, which the `relay` CLI prefers
     over its login), and install the skill at `~/.agents/skills/relay`.
   - Hermes is unchanged: it runs in the team's container, where Relay is always on.

   Each run mints one agent token labelled for this agent, project, and machine, and revokes the
   one it replaces, so the same agent in another project keeps working. A preview (no `--write`)
   mints nothing, and a failed run revokes its new token. Setup refuses the home folder, where
   Codex's project config would be its user-wide one.
3. **The skill gates itself on the tools.** It acts only when MCP tools from a server named
   `relay` are present, and never substitutes the `relay` shell command, which is logged in as
   the human and works in every folder.
4. **Approvals follow where the request came from.** A human asking for an action in their own
   session has approved it. `request_approval` is for writes that came from Relay (an item, a
   teammate's `ask`) or happen while the human is away. The MCP instructions and the agent
   contract say the same.
5. **Labs document, they do not bundle.** `lab-core` points members at `setup-agent`; it does not
   declare Relay as a plugin dependency, because a dependency installs and enables at the
   dependent's scope, which would be every project.

## Alternatives

- **Make Relay a dependency of `lab-core`.** One install for everyone, but Relay would be on in
  every folder, which is the problem this ADR exists to prevent.
- **Copy the skill into `lab-core`.** No cross-repo wiring, but every change to the tool surface
  would have to be copied by hand, and nothing would notice when it was not.
- **Commit the skill into each team repo (`.agents/skills`, `.claude/skills`).** Opt-in by
  presence, but one stale copy per repo.
- **Plugin at the repo root or in `plugins/relay/`.** More conventional paths, but the CLI could
  not read the skill from an installed wheel, which brings back a second copy.
- **Rely on the MCP `instructions` string alone.** It already covers reporting and citing; it
  cannot prevent the CLI fallback when the server is not connected, because then it is not loaded.

## Revisit if

Hermes gains project-scoped MCP or skills; Codex or OpenCode gains a private per-user project
config (then setup-agent should stop writing a file into the repo); Claude Code plugin
dependencies gain a per-project mode (then `lab-core` could depend on Relay); or the skill grows
past what one file can hold.
