# Daily Overview — MCP Server

A local [Model Context Protocol](https://modelcontextprotocol.io) server over the Daily
Overview app, so any MCP client can read and update your planner in natural language:
*"what does my day look like?"*, *"add 'renew insurance' due Friday,"* *"is it going to
rain tomorrow?"*

Built in Python with the official MCP SDK (`FastMCP`). Weather comes from keyless public
APIs — **no API keys to configure.**

## Shared state, not a copy

This server reads and writes the **same** `planner.md` that the dashboard and the Jarvis
spoken briefing use, in the same format, parsed with the same rules as `overview_app.py`.
Add a task here and it shows up in the dashboard; check one off in the dashboard and it
disappears here. There is one source of truth.

## Tools

| Tool | Purpose |
|------|---------|
| `overview_get_tasks` | Today's planner, categorized by urgency |
| `overview_add_task` | Add a to-do, optionally with a due date |
| `overview_complete_task` | Check a task off by fuzzy name match |
| `overview_add_scheduled` | Add a dated (optionally timed) event |
| `overview_remove_item` | Delete an item from any planner section |
| `overview_get_weather` | Current conditions + 1–7 day forecast |
| `overview_briefing` | Weather + planner in one call |

`overview_briefing` is the one to reach for on "how's my day looking" — it saves a
round-trip, and still returns the planner half if the weather lookup fails.

### Categorization

`overview_get_tasks` returns tasks grouped the way the dashboard renders them:

- **overdue** — due date in the past
- **dueToday** / **scheduledToday** — today's tasks and events
- **open** — undated, or due in the future
- **recurring** — daily routine
- **upcoming** — scheduled events in the next 7 days

Completed (`[x]`) items are excluded everywhere.

## Planner format

Unchanged from the app's own convention:

```markdown
## Recurring
- [ ] Check YouTube Studio analytics

## Scheduled
- 2026-09-01 19:00 Pickleball game

## Tasks
- [ ] Renew insurance (due: 2026-09-05)
```

## Weather

Location is auto-detected from your public IP via `ipinfo.io`, then the forecast comes
from `api.open-meteo.com` — the same two services `daily-overview.ps1` uses. Both are
keyless. WMO weather codes are mapped to text with the same table as the PowerShell
script, so wording stays consistent across the app and this server.

These are the only two tools that touch the network; both are annotated
`openWorldHint: true`.

## Setup

Requires Python 3.10+ and the MCP SDK:

```bash
pip install "mcp[cli]"
```

## Register with Claude Code

```bash
claude mcp add daily-overview -- python "C:\Users\anshu\Desktop\Claude\daily-overview-mcp\server.py"
```

## Register with Claude Desktop

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "daily-overview": {
      "command": "python",
      "args": ["C:\\Users\\anshu\\Desktop\\Claude\\daily-overview-mcp\\server.py"]
    }
  }
}
```

Restart Claude Desktop afterwards.

## Test

```bash
python test_server.py
```

30 assertions covering parsing, all five planner-editing tools, date/time validation,
section scoping, file integrity after edits, and the weather path. Uses a temporary
planner — **it never touches your real `planner.md`.**

## Config

Reads `C:\Users\anshu\Desktop\Claude\daily-overview\planner.md` by default. Override with
the `DAILY_OVERVIEW_PLANNER` environment variable.

## Tech

Python · MCP Python SDK (FastMCP) · Pydantic v2 · asyncio · open-meteo · stdlib urllib
