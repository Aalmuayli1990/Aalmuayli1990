# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2", "curl_cffi>=0.7", "beautifulsoup4>=4.12", "lxml>=5.0"]
# ///
"""Smoke test for tasi-filings: MCP client over stdio, no Safari.

Always checks that all 10 tools are registered. The data checks use existing extracts for
symbol 1150, period 2026 Q3 under INVESTMENT_ROOT; when those are absent (e.g. CI) they are
skipped unless REQUIRE_FIXTURE=1.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server.py"
SYMBOL, PERIOD = "1150", "2026 Q3"
REQUIRED = [
    "search_company", "list_filings", "list_local_filings", "ingest_filings",
    "extract_filings", "list_extracted_tables", "get_table", "get_statement_table",
    "get_filing_text", "search_notes",
]


async def _text(result) -> str:
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))


async def _call(session, name, args):
    return await _text(await session.call_tool(name, args))


async def main() -> int:
    params = StdioServerParameters(command="uv", args=["run", str(SERVER)], env={**os.environ})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            print("tools", names)
            assert len(names) == 10 and set(REQUIRED) == set(names), names

            local_raw = await _call(session, "list_local_filings", {"symbol": SYMBOL})
            try:
                local = json.loads(local_raw)
            except json.JSONDecodeError:
                local = {}
            q3 = [f for f in local.get("files", [])
                  if f.get("year") == "2026" and f.get("kind") == "Q3"]
            if not q3:
                msg = f"no extracts for {SYMBOL} {PERIOD} ({local_raw[:200]!r})"
                if os.environ.get("REQUIRE_FIXTURE") == "1":
                    raise AssertionError(msg)
                print(f"\nSKIP data checks: {msg}\nTool registration passed.")
                return 0

            print("\n=== list_local_filings ===")
            assert local["files"], local
            assert q3[0]["extracted"].get("tables_json"), q3[0]

            print("=== get_table income ===")
            args = {"symbol": SYMBOL, "statement": "income", "period": PERIOD}
            stmt = json.loads(await _call(session, "get_table", args))
            assert "error" not in stmt and stmt.get("matches"), stmt
            labels = " ".join(r["label"] for r in stmt["matches"][0]["table"]["rows"])
            assert re.search(r"income|profit|revenue", labels, re.I), labels[:200]
            assert stmt["matches"][0]["filing"].startswith("1150_2026_Q3")

            print("=== get_statement_table == get_table ===")
            alias = json.loads(await _call(session, "get_statement_table", args))
            assert alias == stmt, alias

            print("=== list_extracted_tables ===")
            idx = json.loads(await _call(session, "list_extracted_tables",
                                         {"symbol": SYMBOL, "period": PERIOD}))
            assert idx.get("tables"), idx

    print("\nAll tasi-filings smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
