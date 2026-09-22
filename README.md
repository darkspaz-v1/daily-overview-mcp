# daily-overview-mcp

[![CI](https://github.com/darkspaz-v1/daily-overview-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/darkspaz-v1/daily-overview-mcp/actions/workflows/ci.yml)

An MCP server that lets Claude read and edit your markdown planner and answer "what does my day look like?" with
tasks, schedule and weather in a single tool call.

[Register it](#register-it) · [Example session](#example-session) · [Tools](#tools) · [Architecture](#architecture) · [Tests](#tests)

## Tools

Seven tools, served over stdio:

| Tool | Does | Read-only |
|---|---|---|
| `overview_get_tasks` | Read open tasks from `planner.md`, grouped as overdue / today / open / upcoming | yes |
| `overview_add_task` | Append a task, optionally with a due date | no |
| `overview_complete_task` | Mark one done | no |
| `overview_add_scheduled` | Add a dated/timed item | no |
| `overview_remove_item` | Remove a task or scheduled item | no |
| `overview_get_weather` | IP-geolocated forecast, `days` configurable | yes |
| `overview_briefing` | Weather plus the day's tasks in one call | yes |

The "Read-only" column is each tool's `readOnlyHint` annotation in `server.py`. The write tools edit your
`planner.md` in place.

## Register it

The server is started by your MCP client, not by hand. Use the Python from the environment where you ran
`pip install -r requirements.txt` (for a venv, the full path to `.venv\Scripts\python.exe`) as `command`.

Claude Desktop reads `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "daily-overview": {
      "command": "python",
      "args": ["C:/path/to/daily-overview-mcp/server.py"],
      "env": { "DAILY_OVERVIEW_PLANNER": "C:/path/to/planner.md" }
    }
  }
}
```

Claude Code equivalent:

```
claude mcp add daily-overview -e DAILY_OVERVIEW_PLANNER=C:/path/to/planner.md -- python C:/path/to/daily-overview-mcp/server.py
```

**Register it twice if you use both Claude Code and Claude Desktop.** They read separate config files,
and a server registered in one is invisible to the other — this cost real debugging time.

## Install

Requires Python 3.11 or newer (verified on 3.13).

```
git clone https://github.com/darkspaz-v1/daily-overview-mcp.git
cd daily-overview-mcp
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python test_server.py
```

`pip install .` also works and installs the same two dependencies (`mcp`, `pydantic`). For linting
(`ruff check .`, as CI does), use `pip install -r requirements-dev.txt` instead.

## Configuration

| Environment variable | Meaning | Default |
|---|---|---|
| `DAILY_OVERVIEW_PLANNER` | Path to your `planner.md` (sections `## Recurring`, `## Scheduled`, `## Tasks`). | `~/Desktop/Claude/daily-overview/planner.md` |

Set the variable in your MCP client config (above); the default is only a convenience fallback.

## Example session

_Example output._ Captured by running `python examples/demo_client.py`, which starts `server.py` over stdio, points it at a
throwaway planner of invented data (dates are relative to the day you run it), and calls three tools. Your real
`planner.md` is not involved. The weather tools are left out because their output depends on your IP-derived location.

```text
TOOLS ['overview_get_tasks', 'overview_add_task', 'overview_complete_task', 'overview_add_scheduled', 'overview_remove_item', 'overview_get_weather', 'overview_briefing']

>>> overview_get_tasks {}
# Planner — 2026-09-21

## Overdue
- Submit lab report 3 (was due 2026-09-19)

## Scheduled today
- 09:30 - Team standup
- 13:00 - Lunch with Sam
- 16:15 - Study group: Intro to Algorithms

## Due today
- Finish problem set 4
- Reply to landlord about the lease renewal

## Open tasks
- Draft project proposal outline (due 2026-09-25)
- Renew library books (due 2026-09-28)
- Buy a birthday gift for Alex
- Organize the photo backup folder

## Daily routine
- Morning stretch and water
- Review inbox for 10 minutes
- Evening walk

## Upcoming (next 7 days)
- 2026-09-22 10:00 Dentist appointment
- 2026-09-24 18:30 Pottery class
- 2026-09-26 11:00 Farmers market run

_7 open task(s) total._

>>> overview_add_task {"text": "Book train tickets", "due": "2026-09-30"}
Added task: Book train tickets (due 2026-09-30)

>>> overview_complete_task {"query": "problem set"}
Completed: Finish problem set 4 (due: 2026-09-21)
```

Each `>>>` line is the tool name and the arguments the client sent; everything below it is the text the server returned.
Read tools also accept `response_format: "json"` to return the same data as JSON instead of markdown.

## Architecture

```mermaid
flowchart LR
    Client["MCP client<br/>Claude Code / Claude Desktop"] <-->|stdio| Server["server.py<br/>FastMCP"]
    Server --> T1["overview_get_tasks"]
    Server --> T2["overview_add_task"]
    Server --> T3["overview_complete_task"]
    Server --> T4["overview_add_scheduled"]
    Server --> T5["overview_remove_item"]
    Server --> T6["overview_get_weather"]
    Server --> T7["overview_briefing"]
    T1 --> Planner[("planner.md")]
    T2 --> Planner
    T3 --> Planner
    T4 --> Planner
    T5 --> Planner
    T6 --> Weather(["ipinfo.io + open-meteo.com"])
    T7 --> Planner
    T7 --> Weather
```

## The design decision worth stating

The source of truth is a **plain markdown file a human edits by hand**, not a database. That means the
server has to tolerate a file that changed underneath it and formatting that a person wrote, which is
the real constraint — it parses and rewrites in place rather than owning the format.

`overview_briefing` exists because the common request ("what does my day look like?") otherwise costs
two round trips and a client-side join.

## Notes

- Tests run against a **temporary planner file**, so they never touch a real `planner.md`.
- The weather tools need network and use your IP address to find your approximate location (ipinfo.io, then
  open-meteo.com for the forecast). If the lookup fails, the test asserts the **error path** behaved
  correctly instead of failing the run — a test suite that only passes online is not a test suite.

## About MCP

[Model Context Protocol](https://modelcontextprotocol.io) is a standard for exposing tools to an LLM
client. This server speaks MCP over stdio, so it is registered in the client config rather than run
directly (see [Register it](#register-it)).

## Tests

```
python test_server.py
```

Drives every tool through the real handlers and prints one `PASS` line per check (35 at the time of writing). No
pytest — the suite is a single script so it runs anywhere with no dev dependencies. CI runs it, plus `ruff check .`,
on Windows with Python 3.12 and 3.13.

## License

MIT — see [LICENSE](LICENSE).
