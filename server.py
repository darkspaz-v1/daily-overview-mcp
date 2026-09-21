#!/usr/bin/env python3
"""
Daily Overview MCP Server.

A local MCP server over the Daily Overview app's planner.md
(set DAILY_OVERVIEW_PLANNER to its path), exposing your planner and
live weather to any MCP client — so any assistant can read and update your
tasks instead of hand-editing planner.md.

This server reads and writes the SAME planner.md the dashboard and the Jarvis
briefing use, in the same format, so all three stay in sync. It parses the
three `##` sections (Recurring / Scheduled / Tasks) with the same rules as
overview_app.py:

    Recurring   - [ ] text            (or plain "- text")
    Scheduled   - YYYY-MM-DD [HH:MM] what
    Tasks       - [ ] text (due: YYYY-MM-DD)

Weather is fetched live from open-meteo.com, with the location auto-detected
from your public IP via ipinfo.io — the same two services daily-overview.ps1
uses. Both are keyless, so nothing needs configuring.

Tools:
    - overview_get_tasks       Today's planner, categorized (overdue/due/open/...).
    - overview_add_task        Add a to-do, optionally with a due date.
    - overview_complete_task   Check a task off by fuzzy name match.
    - overview_add_scheduled   Add a dated (and optionally timed) event.
    - overview_remove_item     Delete an item from any planner section.
    - overview_get_weather     Current conditions plus a multi-day forecast.
    - overview_briefing        Weather + planner in one combined summary.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import threading
import urllib.request
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration & storage
# ---------------------------------------------------------------------------

mcp = FastMCP("dailyoverview_mcp")
log = logging.getLogger("dailyoverview_mcp")  # stdio transport: logs go to stderr, never stdout

# Path to the planner markdown file. Set DAILY_OVERVIEW_PLANNER; the fallback is
# <home>/Desktop/Claude/daily-overview/planner.md (home = %USERPROFILE% on Windows).
PLANNER_FILE = os.environ.get(
    "DAILY_OVERVIEW_PLANNER",
    os.path.join(
        os.path.expanduser("~"), "Desktop", "Claude", "daily-overview", "planner.md"
    ),
)

# Network timeout (seconds) for the two weather lookups.
_HTTP_TIMEOUT = 15

# A single lock guards all planner reads/writes so concurrent calls stay consistent.
_LOCK = threading.Lock()

# WMO weather codes -> text (mirrors Weather-Desc in daily-overview.ps1).
WCODE = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle", 57: "Dense freezing drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 66: "Freezing rain", 67: "Heavy freezing rain", 71: "Light snow",
    73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Light showers", 81: "Showers",
    82: "Violent showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
}


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


class Section(str, Enum):
    """The three planner sections."""

    TASKS = "tasks"
    SCHEDULED = "scheduled"
    RECURRING = "recurring"


# ---------------------------------------------------------------------------
# Planner helpers
# ---------------------------------------------------------------------------


def _read_lines() -> List[str]:
    """Read planner.md as a list of lines (no trailing newlines)."""
    if not os.path.exists(PLANNER_FILE):
        return []
    with open(PLANNER_FILE, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def _write_lines(lines: List[str]) -> None:
    """Persist planner.md atomically."""
    tmp = f"{PLANNER_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, PLANNER_FILE)


def _section_bounds(lines: List[str], keyword: str) -> Tuple[Optional[int], int]:
    """Locate a `##` section by keyword.

    Returns (heading_index, end_index) where end_index is the line index of the
    next `##` heading (or len(lines)). Returns (None, len(lines)) if not found.
    """
    start = None
    for i, raw in enumerate(lines):
        m = re.match(r"^\s*##\s*(.+?)\s*$", raw)
        if not m:
            continue
        if start is None and keyword.lower() in m.group(1).lower():
            start = i
        elif start is not None:
            return start, i
    return start, len(lines)


def _last_item_index(lines: List[str], start: int, end: int) -> int:
    """Index just after the last list item in a section, for tidy insertion."""
    last = start
    for i in range(start + 1, end):
        if lines[i].strip().startswith("-"):
            last = i
    return last + 1


def _match_line(
    lines: List[str], start: int, end: int, query: str, open_only: bool
) -> Tuple[Optional[int], Optional[str]]:
    """Find the first list item in a section whose text contains `query`.

    When open_only, only unchecked `- [ ]` items are considered.
    Returns (line_index, item_text) or (None, None).
    """
    q = (query or "").lower().strip()
    pattern = r"^-\s*\[( )\]\s*(.+)$" if open_only else r"^-\s*(?:\[[ xX]\]\s*)?(.+)$"
    group = 2 if open_only else 1
    for i in range(start + 1, end):
        m = re.match(pattern, lines[i].strip())
        if m and (q in m.group(group).lower()):
            return i, m.group(group).strip()
    return None, None


def _categorized() -> Dict[str, Any]:
    """Parse the whole planner into the shape the dashboard renders.

    Mirrors DailyOverview.categorized() in overview_app.py so both agree.
    """
    today = datetime.date.today()
    recurring: List[str] = []
    sched_today: List[str] = []
    upcoming: List[str] = []
    due_today: List[str] = []
    overdue: List[str] = []
    open_tasks: List[str] = []
    section = ""

    for raw in _read_lines():
        m = re.match(r"^\s*##\s*(.+?)\s*$", raw)
        if m:
            section = m.group(1).lower()
            continue
        t = raw.strip()
        if not t or t.startswith("#"):
            continue

        if "recurring" in section:
            mm = re.match(r"^-\s*\[( )\]\s*(.+)$", t)
            if mm:
                recurring.append(mm.group(2).strip())
            elif re.match(r"^-\s*\[[xX]\]", t):
                pass
            elif re.match(r"^-\s*(.+)$", t):
                recurring.append(re.match(r"^-\s*(.+)$", t).group(1).strip())

        elif "scheduled" in section:
            mm = re.match(
                r"^-\s*(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?\s+(.+)$", t
            )
            if not mm:
                continue
            try:
                dd = datetime.date.fromisoformat(mm.group(1))
            except ValueError:
                log.warning("Skipping scheduled line with invalid date: %r", t)
                continue
            tm, what = mm.group(2), mm.group(3).strip()
            if dd == today:
                sched_today.append((tm + " - " if tm else "") + what)
            elif today < dd <= today + datetime.timedelta(days=7):
                upcoming.append(
                    (mm.group(1) + " " + (tm + " " if tm else "") + what).strip()
                )

        elif "task" in section:
            mm = re.match(r"^-\s*\[( )\]\s*(.+)$", t)
            if not mm:
                continue
            body, due = mm.group(2).strip(), None
            dm = re.search(r"\(due:\s*(\d{4}-\d{2}-\d{2})\)", body)
            if dm:
                due = dm.group(1)
                body = re.sub(r"\s*\(due:\s*\d{4}-\d{2}-\d{2}\)", "", body).strip()
            if not due:
                open_tasks.append(body)
                continue
            try:
                dd = datetime.date.fromisoformat(due)
            except ValueError:
                log.warning("Task has invalid due date %r, treating as open: %r", due, t)
                dd = None
            if dd == today:
                due_today.append(body)
            elif dd and dd < today:
                overdue.append(f"{body} (was due {due})")
            else:
                open_tasks.append(f"{body} (due {due})")

    return {
        "date": today.isoformat(),
        "overdue": overdue,
        "scheduledToday": sched_today,
        "dueToday": due_today,
        "recurring": recurring,
        "open": open_tasks,
        "upcoming": upcoming,
        "openCount": len(overdue) + len(due_today) + len(open_tasks),
    }


def _format_tasks_markdown(data: Dict[str, Any]) -> str:
    """Render the categorized planner as readable markdown."""
    lines = [f"# Planner — {data['date']}", ""]
    blocks = [
        ("Overdue", data["overdue"]),
        ("Scheduled today", data["scheduledToday"]),
        ("Due today", data["dueToday"]),
        ("Open tasks", data["open"]),
        ("Daily routine", data["recurring"]),
        ("Upcoming (next 7 days)", data["upcoming"]),
    ]
    any_content = False
    for title, entries in blocks:
        if not entries:
            continue
        any_content = True
        lines.append(f"## {title}")
        for entry in entries:
            lines.append(f"- {entry}")
        lines.append("")
    if not any_content:
        return f"# Planner — {data['date']}\n\nNothing on the planner. All clear."
    lines.append(f"_{data['openCount']} open task(s) total._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Weather helpers
# ---------------------------------------------------------------------------


def _get_json(url: str) -> Any:
    """Fetch and parse a JSON URL, raising OSError on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "daily-overview-mcp"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_weather(days: int) -> Dict[str, Any]:
    """Locate the user by public IP, then fetch current + daily forecast.

    Returns {"ok": False, "error": ...} rather than raising, so tools can turn
    the failure into an actionable message.
    """
    place, lat, lon = "your location", None, None
    try:
        geo = _get_json("https://ipinfo.io/json")
        if geo.get("loc"):
            lat, lon = geo["loc"].split(",")
            place = ", ".join(
                p for p in (geo.get("city"), geo.get("region"), geo.get("country")) if p
            )
    except (OSError, ValueError, KeyError) as exc:
        return {
            "ok": False,
            "error": (
                f"Could not determine your location from ipinfo.io ({exc}). "
                f"Check your internet connection and try again."
            ),
        }

    if lat is None or lon is None:
        return {"ok": False, "error": "ipinfo.io returned no coordinates."}

    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,apparent_temperature,weather_code,"
        "wind_speed_10m,relative_humidity_2m"
        "&daily=temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,weather_code,sunrise,sunset"
        f"&timezone=auto&forecast_days={days}"
    )
    try:
        raw = _get_json(url)
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "error": f"Could not reach the open-meteo forecast API ({exc}).",
        }

    cur = raw.get("current", {}) or {}
    code = int(cur.get("weather_code", 0))
    daily_raw = raw.get("daily", {}) or {}
    daily: List[Dict[str, Any]] = []
    for i, day in enumerate(daily_raw.get("time", [])):
        d_code = int(daily_raw["weather_code"][i])
        daily.append(
            {
                "date": day,
                "high_c": daily_raw["temperature_2m_max"][i],
                "low_c": daily_raw["temperature_2m_min"][i],
                "precip_chance_pct": daily_raw["precipitation_probability_max"][i],
                "code": d_code,
                "description": WCODE.get(d_code, f"Weather code {d_code}"),
                "sunrise": daily_raw["sunrise"][i],
                "sunset": daily_raw["sunset"][i],
            }
        )

    return {
        "ok": True,
        "place": place,
        "current": {
            "temp_c": cur.get("temperature_2m"),
            "feels_like_c": cur.get("apparent_temperature"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "code": code,
            "description": WCODE.get(code, f"Weather code {code}"),
        },
        "daily": daily,
    }


def _format_weather_markdown(w: Dict[str, Any]) -> str:
    """Render a successful weather payload as readable markdown."""
    cur = w["current"]
    lines = [
        f"# Weather — {w['place']}",
        "",
        f"**Now:** {cur['description']}, {cur['temp_c']}°C "
        f"(feels like {cur['feels_like_c']}°C)",
        f"- Humidity {cur['humidity_pct']}% · Wind {cur['wind_kmh']} km/h",
    ]
    if w["daily"]:
        lines += ["", "## Forecast"]
        for d in w["daily"]:
            lines.append(
                f"- **{d['date']}** — {d['description']}, "
                f"{d['low_c']}–{d['high_c']}°C, {d['precip_chance_pct']}% rain"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class GetTasksInput(BaseModel):
    """Input for reading the planner."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


class AddTaskInput(BaseModel):
    """Input for adding a to-do item."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    text: str = Field(
        ..., description="The task text (e.g. 'Renew car insurance')",
        min_length=1, max_length=500,
    )
    due: Optional[str] = Field(
        default=None,
        description="Optional due date as YYYY-MM-DD (e.g. '2026-09-01')",
        max_length=10,
    )

    @field_validator("due")
    @classmethod
    def _valid_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        try:
            datetime.date.fromisoformat(v.strip())
        except ValueError:
            raise ValueError(f"due must be YYYY-MM-DD, got '{v}'") from None
        return v.strip()


class CompleteTaskInput(BaseModel):
    """Input for checking a task off."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description=(
            "Text to match against open tasks (case-insensitive substring). "
            "The first open task containing it is checked off."
        ),
        min_length=1,
        max_length=200,
    )


class AddScheduledInput(BaseModel):
    """Input for adding a dated event."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    date: str = Field(..., description="Event date as YYYY-MM-DD", max_length=10)
    text: str = Field(
        ..., description="What the event is (e.g. 'Dentist appointment')",
        min_length=1, max_length=500,
    )
    time: Optional[str] = Field(
        default=None, description="Optional 24-hour time as HH:MM (e.g. '19:00')",
        max_length=5,
    )

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        try:
            datetime.date.fromisoformat(v.strip())
        except ValueError:
            raise ValueError(f"date must be YYYY-MM-DD, got '{v}'") from None
        return v.strip()

    @field_validator("time")
    @classmethod
    def _valid_time(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        if not re.match(r"^\d{1,2}:\d{2}$", v.strip()):
            raise ValueError(f"time must be HH:MM, got '{v}'")
        return v.strip()


class RemoveItemInput(BaseModel):
    """Input for deleting a planner line."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ..., description="Text to match against planner items (case-insensitive)",
        min_length=1, max_length=200,
    )
    section: Optional[Section] = Field(
        default=None,
        description=(
            "Restrict the search to one section: 'tasks', 'scheduled', or "
            "'recurring'. Omit to search all three."
        ),
    )


class WeatherInput(BaseModel):
    """Input for the weather lookup."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    days: int = Field(
        default=3,
        description="How many forecast days to return, including today (1-7)",
        ge=1,
        le=7,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


class BriefingInput(BaseModel):
    """Input for the combined briefing."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="overview_get_tasks",
    annotations={
        "title": "Get Planner Tasks",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def overview_get_tasks(params: GetTasksInput) -> str:
    """Read the planner, categorized by urgency for today.

    Completed (`[x]`) tasks are excluded. Scheduled events are split into
    today's and the next 7 days.

    Args:
        params (GetTasksInput): Validated input containing:
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: In json, an object of the form:
            {
              "date": "YYYY-MM-DD",
              "overdue": [str], "scheduledToday": [str], "dueToday": [str],
              "recurring": [str], "open": [str], "upcoming": [str],
              "openCount": int
            }
            In markdown, the same content grouped under headings.
    """
    with _LOCK:
        if not os.path.exists(PLANNER_FILE):
            return (
                f"Error: Planner not found at {PLANNER_FILE}. Set the "
                f"DAILY_OVERVIEW_PLANNER environment variable to its real location."
            )
        data = _categorized()

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2, ensure_ascii=False)
    return _format_tasks_markdown(data)


@mcp.tool(
    name="overview_add_task",
    annotations={
        "title": "Add Task to Planner",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def overview_add_task(params: AddTaskInput) -> str:
    """Add a to-do item to the planner's Tasks section.

    Written in the app's own format (`- [ ] text (due: YYYY-MM-DD)`) so the
    dashboard and spoken briefing pick it up unchanged.

    Args:
        params (AddTaskInput): Validated input containing:
            - text (str): Task text.
            - due (Optional[str]): Due date as YYYY-MM-DD.

    Returns:
        str: Confirmation, or an actionable error if the Tasks section is missing.
    """
    with _LOCK:
        lines = _read_lines()
        start, end = _section_bounds(lines, "Task")
        if start is None:
            return (
                f"Error: No '## Tasks' section found in {PLANNER_FILE}. "
                f"Add that heading to the file and try again."
            )
        entry = f"- [ ] {params.text}"
        if params.due:
            entry += f" (due: {params.due})"
        lines.insert(_last_item_index(lines, start, end), entry)
        _write_lines(lines)

    suffix = f" (due {params.due})" if params.due else ""
    return f"Added task: {params.text}{suffix}"


@mcp.tool(
    name="overview_complete_task",
    annotations={
        "title": "Complete Planner Task",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def overview_complete_task(params: CompleteTaskInput) -> str:
    """Check off the first open task matching a search string.

    Flips `- [ ]` to `- [x]`, which drops the task out of the overview without
    deleting it.

    Args:
        params (CompleteTaskInput): Validated input containing:
            - query (str): Text to match against open tasks.

    Returns:
        str: Confirmation naming the completed task, or an actionable error
             listing what is currently open.
    """
    with _LOCK:
        lines = _read_lines()
        start, end = _section_bounds(lines, "Task")
        if start is None:
            return f"Error: No '## Tasks' section found in {PLANNER_FILE}."
        idx, body = _match_line(lines, start, end, params.query, open_only=True)
        if idx is None:
            open_now = _categorized()["open"]
            hint = (
                f" Currently open: {', '.join(repr(t) for t in open_now[:10])}."
                if open_now
                else " There are no open tasks."
            )
            return f"Error: No open task matching '{params.query}'.{hint}"
        lines[idx] = lines[idx].replace("[ ]", "[x]", 1)
        _write_lines(lines)

    return f"Completed: {body}"


@mcp.tool(
    name="overview_add_scheduled",
    annotations={
        "title": "Add Scheduled Event",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def overview_add_scheduled(params: AddScheduledInput) -> str:
    """Add a dated event to the planner's Scheduled section.

    Events show on their day and are previewed for the next 7 days.

    Args:
        params (AddScheduledInput): Validated input containing:
            - date (str): Event date as YYYY-MM-DD.
            - text (str): What the event is.
            - time (Optional[str]): 24-hour HH:MM.

    Returns:
        str: Confirmation, or an actionable error if the section is missing.
    """
    with _LOCK:
        lines = _read_lines()
        start, end = _section_bounds(lines, "Scheduled")
        if start is None:
            return (
                f"Error: No '## Scheduled' section found in {PLANNER_FILE}. "
                f"Add that heading to the file and try again."
            )
        stamp = f"{params.date} {params.time}" if params.time else params.date
        lines.insert(_last_item_index(lines, start, end), f"- {stamp} {params.text}")
        _write_lines(lines)

    return f"Scheduled: {stamp} — {params.text}"


@mcp.tool(
    name="overview_remove_item",
    annotations={
        "title": "Remove Planner Item",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def overview_remove_item(params: RemoveItemInput) -> str:
    """Permanently delete the first planner line matching a search string.

    Unlike overview_complete_task, this removes the line entirely. Prefer
    completing tasks you actually finished, so the record is kept.

    Args:
        params (RemoveItemInput): Validated input containing:
            - query (str): Text to match.
            - section (Optional[Section]): Restrict to one section.

    Returns:
        str: Confirmation naming the removed line, or an actionable error.
    """
    keywords = (
        [params.section.value] if params.section else ["Task", "Scheduled", "Recurring"]
    )
    with _LOCK:
        lines = _read_lines()
        for keyword in keywords:
            start, end = _section_bounds(lines, keyword)
            if start is None:
                continue
            idx, body = _match_line(lines, start, end, params.query, open_only=False)
            if idx is None:
                continue
            del lines[idx]
            _write_lines(lines)
            return f"Removed: {body}"

    scope = f" in the {params.section.value} section" if params.section else ""
    return (
        f"Error: Nothing matching '{params.query}'{scope}. "
        f"Use overview_get_tasks to see what is on the planner."
    )


@mcp.tool(
    name="overview_get_weather",
    annotations={
        "title": "Get Weather Forecast",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def overview_get_weather(params: WeatherInput) -> str:
    """Get current conditions and a daily forecast for your current location.

    Location is auto-detected from your public IP; no API key is needed. Always
    answer weather questions from these numbers rather than estimating.

    Args:
        params (WeatherInput): Validated input containing:
            - days (int): Forecast days including today (1-7, default 3).
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: In json, an object of the form:
            {
              "ok": true, "place": str,
              "current": {"temp_c", "feels_like_c", "humidity_pct",
                          "wind_kmh", "code", "description"},
              "daily": [ {"date", "high_c", "low_c", "precip_chance_pct",
                          "code", "description", "sunrise", "sunset"}, ... ]
            }
            In markdown, the same reading conditions and forecast.
    """
    w = _fetch_weather(params.days)

    if not w.get("ok"):
        return f"Error: {w.get('error', 'weather lookup failed')}"

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(w, indent=2, ensure_ascii=False)
    return _format_weather_markdown(w)


@mcp.tool(
    name="overview_briefing",
    annotations={
        "title": "Daily Briefing",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def overview_briefing(params: BriefingInput) -> str:
    """Produce the full daily briefing: weather plus today's planner in one call.

    This is the tool to use for "what does my day look like" — it saves the
    round-trip of calling the weather and planner tools separately. If the
    weather lookup fails, the planner half is still returned.

    Args:
        params (BriefingInput): Validated input containing:
            - response_format (ResponseFormat): 'markdown' or 'json'.

    Returns:
        str: In json, {"weather": {...}, "planner": {...}} using the same shapes
             as overview_get_weather and overview_get_tasks. In markdown, a
             combined briefing.
    """
    with _LOCK:
        planner = _categorized() if os.path.exists(PLANNER_FILE) else None
    weather = _fetch_weather(2)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(
            {"weather": weather, "planner": planner}, indent=2, ensure_ascii=False
        )

    parts: List[str] = []
    if weather.get("ok"):
        cur = weather["current"]
        parts.append(
            f"# Briefing — {datetime.date.today().isoformat()}\n\n"
            f"**{weather['place']}:** {cur['description']}, {cur['temp_c']}°C "
            f"(feels like {cur['feels_like_c']}°C)."
        )
        if weather["daily"]:
            d = weather["daily"][0]
            parts.append(
                f"Today {d['low_c']}–{d['high_c']}°C, "
                f"{d['precip_chance_pct']}% chance of rain."
            )
    else:
        parts.append(
            f"# Briefing — {datetime.date.today().isoformat()}\n\n"
            f"_Weather unavailable: {weather.get('error')}_"
        )

    if planner is None:
        parts.append(f"\n_Planner not found at {PLANNER_FILE}._")
    else:
        parts.append("\n" + _format_tasks_markdown(planner))

    return "\n".join(parts)


if __name__ == "__main__":
    mcp.run()
