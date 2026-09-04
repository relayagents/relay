# ADR-0006: workspace-mcp over Google's preview MCP servers

**Status:** accepted · 2026-09-03

## Context

Agents need Docs, Drive, Calendar, and Gmail under each user's own consent, without Relay ever holding Google tokens.

## Decision

Run `taylorwilsdon/google_workspace_mcp` as the `workspace-mcp` container in multi-user OAuth 2.1 mode. Relay is an MCP client of it (`connectors/workspace`) and user agents can point at it directly. Google tokens live only in that container's credential store.

## Alternatives

- **Google's own preview MCP servers.** Promising but preview-only, uneven coverage across products, and less clear multi-user self-hosting at the time of writing.
- **Direct Google API client inside Relay.** Would make Relay hold OAuth tokens, against ADR-0002.

## Revisit if

Google ships supported, self-hostable MCP servers with multi-user OAuth, or `workspace-mcp` goes unmaintained.
