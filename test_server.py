#!/usr/bin/env python3
"""End-to-end smoke test: drives every tool through the real handlers.

Uses a temporary planner file so it never touches your real planner.md.
The weather tools need internet; if the lookup fails the test reports that
the error path behaved correctly rather than failing the run.

Run:  python test_server.py
"""

import asyncio
import datetime
import importlib
import json
import os
import tempfile

# Point the server at a throwaway planner BEFORE importing it.
_tmp = tempfile.mkdtemp()
_planner = os.path.join(_tmp, "planner.md")

TODAY = datetime.date.today()
YESTERDAY = (TODAY - datetime.timedelta(days=1)).isoformat()
TOMORROW = (TODAY + datetime.timedelta(days=1)).isoformat()

with open(_planner, "w", encoding="utf-8") as fh:
    fh.write(
        "# Planner\n\n"
        "## Recurring\n"
        "- [ ] Check YouTube Studio analytics\n"
        "- [x] Already done today\n\n"
        "## Scheduled\n"
        f"- {TODAY.isoformat()} 19:00 Pickleball game\n"
        f"- {TOMORROW} call to school\n\n"
        "## Tasks\n"
        f"- [ ] Renew insurance (due: {YESTERDAY})\n"
        f"- [ ] Ship the MCP servers (due: {TODAY.isoformat()})\n"
        "- [ ] Something with no due date\n"
        "- [x] Finished thing\n"
    )
os.environ["DAILY_OVERVIEW_PLANNER"] = _planner

import server as s  # noqa: E402


def ok(cond: bool, label: str) -> None:
    print(("PASS" if cond else "FAIL"), "-", label)
    assert cond, label


async def main() -> None:
    J = s.ResponseFormat.JSON

    # 1. Categorization matches the dashboard's rules
    d = json.loads(await s.overview_get_tasks(s.GetTasksInput(response_format=J)))
    ok(any("Renew insurance" in t for t in d["overdue"]), "past due date -> overdue")
    ok("Ship the MCP servers" in d["dueToday"], "today's due date -> dueToday")
    ok("Something with no due date" in d["open"], "undated task -> open")
    ok("Check YouTube Studio analytics" in d["recurring"], "recurring parsed")
    ok(not any("Already done" in t for t in d["recurring"]), "checked recurring excluded")
    ok(not any("Finished thing" in t for t in d["open"]), "checked task excluded")
    ok("19:00 - Pickleball game" in d["scheduledToday"], "today's event, time-prefixed")
    ok(any("call to school" in u for u in d["upcoming"]), "tomorrow -> upcoming")
    ok(d["openCount"] == 3, "openCount counts overdue + dueToday + open")

    # 2. Add a task with and without a due date
    r = await s.overview_add_task(s.AddTaskInput(text="Buy milk"))
    ok("Buy milk" in r, "add_task confirms")
    r = await s.overview_add_task(s.AddTaskInput(text="File taxes", due=TOMORROW))
    ok(TOMORROW in r, "add_task echoes the due date")

    d = json.loads(await s.overview_get_tasks(s.GetTasksInput(response_format=J)))
    ok("Buy milk" in d["open"], "added task appears as open")
    ok(any("File taxes" in t for t in d["open"]), "dated future task appears as open")

    # 3. Bad dates are rejected before touching the file
    try:
        s.AddTaskInput(text="Bad", due="not-a-date")
        ok(False, "invalid due date rejected")
    except Exception:
        ok(True, "invalid due date rejected")

    try:
        s.AddScheduledInput(date=TOMORROW, text="Bad time", time="9pm")
        ok(False, "invalid time rejected")
    except Exception:
        ok(True, "invalid time rejected")

    # 4. Complete a task
    r = await s.overview_complete_task(s.CompleteTaskInput(query="buy milk"))
    ok("Buy milk" in r, "complete matches case-insensitively")
    d = json.loads(await s.overview_get_tasks(s.GetTasksInput(response_format=J)))
    ok("Buy milk" not in d["open"], "completed task drops out")

    r = await s.overview_complete_task(s.CompleteTaskInput(query="buy milk"))
    ok("No open task matching" in r, "completing twice is caught")

    # 5. Scheduled events
    r = await s.overview_add_scheduled(
        s.AddScheduledInput(date=TOMORROW, text="Dentist", time="09:30")
    )
    ok("Dentist" in r, "add_scheduled confirms")
    d = json.loads(await s.overview_get_tasks(s.GetTasksInput(response_format=J)))
    ok(any("Dentist" in u for u in d["upcoming"]), "scheduled event shows in upcoming")

    # 6. Removal, including section scoping
    r = await s.overview_remove_item(s.RemoveItemInput(query="Dentist"))
    ok("Removed" in r, "remove_item confirms")

    r = await s.overview_remove_item(
        s.RemoveItemInput(query="Pickleball", section=s.Section.TASKS)
    )
    ok("Nothing matching" in r, "section scoping prevents cross-section removal")

    r = await s.overview_remove_item(
        s.RemoveItemInput(query="Pickleball", section=s.Section.SCHEDULED)
    )
    ok("Removed" in r, "correct section removes it")

    r = await s.overview_remove_item(s.RemoveItemInput(query="zzz-nothing"))
    ok("Nothing matching" in r, "missing item gives actionable error")

    # 7. The planner file is still valid markdown the dashboard can parse
    with open(_planner, encoding="utf-8") as fh:
        text = fh.read()
    ok(text.count("## Tasks") == 1, "Tasks heading intact after edits")
    ok(text.count("## Scheduled") == 1, "Scheduled heading intact after edits")
    ok(text.endswith("\n"), "file ends with a newline")

    # 8. Weather / briefing — network dependent
    w = await s.overview_get_weather(s.WeatherInput(days=2, response_format=J))
    if w.startswith("Error:"):
        ok(True, "weather offline -> actionable error (network unavailable)")
    else:
        wd = json.loads(w)
        ok(wd["ok"] and wd["current"]["description"], "weather returns current conditions")
        ok(len(wd["daily"]) == 2, "weather honours the days parameter")

    b = await s.overview_briefing(s.BriefingInput())
    ok("Briefing" in b and "Planner" in b, "briefing combines weather and planner")

    # 9. Hand-edited planners with impossible dates are skipped/degraded, not fatal
    with open(_planner, "w", encoding="utf-8") as fh:
        fh.write(
            "# Planner\n\n## Recurring\n\n"
            "## Scheduled\n- 2026-13-45 impossible date\n\n"
            "## Tasks\n- [ ] Bad due (due: 2026-02-31)\n- [ ] Fine task\n"
        )
    d = json.loads(await s.overview_get_tasks(s.GetTasksInput(response_format=J)))
    ok(d["scheduledToday"] == [] and d["upcoming"] == [], "impossible scheduled date skipped")
    ok(any("Bad due" in t for t in d["open"]), "impossible due date falls back to open")
    ok("Fine task" in d["open"], "valid tasks unaffected by malformed neighbours")

    # 10. Configuration: the env var wins, and the fallback is home-relative
    ok(s.PLANNER_FILE == _planner, "DAILY_OVERVIEW_PLANNER env var sets the planner path")
    del os.environ["DAILY_OVERVIEW_PLANNER"]
    try:
        s2 = importlib.reload(s)
        ok(
            s2.PLANNER_FILE
            == os.path.join(
                os.path.expanduser("~"), "Desktop", "Claude", "daily-overview", "planner.md"
            ),
            "fallback planner path is derived from the home directory",
        )
    finally:
        os.environ["DAILY_OVERVIEW_PLANNER"] = _planner
        importlib.reload(s)

    print("\nAll daily-overview tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
