#!/usr/bin/env python3
"""Drive the server over stdio the way an MCP client does, against an invented planner.

Writes a throwaway planner (dates relative to today, so the output is reproducible on any day),
starts server.py as a subprocess, and prints each tool call and the text it returns.
Your real planner.md is never read. Only the offline planner tools are called; the weather
tools are left out because their output depends on your IP-derived location.

Run from the repo root:  python examples/demo_client.py
"""

import asyncio
import datetime
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = datetime.date.today()


def day(offset: int) -> str:
    return (TODAY + datetime.timedelta(days=offset)).isoformat()


PLANNER = f"""# Planner (invented example data)

## Recurring
- Morning stretch and water
- Review inbox for 10 minutes
- Evening walk

## Scheduled
- {day(0)} 09:30 Team standup
- {day(0)} 13:00 Lunch with Sam
- {day(0)} 16:15 Study group: Intro to Algorithms
- {day(1)} 10:00 Dentist appointment
- {day(3)} 18:30 Pottery class
- {day(5)} 11:00 Farmers market run

## Tasks
- [ ] Submit lab report 3 (due: {day(-2)})
- [ ] Finish problem set 4 (due: {day(0)})
- [ ] Reply to landlord about the lease renewal (due: {day(0)})
- [ ] Draft project proposal outline (due: {day(4)})
- [ ] Renew library books (due: {day(7)})
- [ ] Buy a birthday gift for Alex
- [ ] Organize the photo backup folder
- [x] Book dentist appointment
"""

CALLS = [
    ("overview_get_tasks", {}),
    ("overview_add_task", {"text": "Book train tickets", "due": day(9)}),
    ("overview_complete_task", {"query": "problem set"}),
]


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        planner = os.path.join(tmp, "planner.md")
        with open(planner, "w", encoding="utf-8") as fh:
            fh.write(PLANNER)
        params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.join(ROOT, "server.py")],
            env={**os.environ, "DAILY_OVERVIEW_PLANNER": planner},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print("TOOLS", [t.name for t in tools.tools])
                for name, args in CALLS:
                    result = await session.call_tool(name, {"params": args})
                    print(f"\n>>> {name} {json.dumps(args)}")
                    for block in result.content:
                        print(block.text)


if __name__ == "__main__":
    asyncio.run(main())
