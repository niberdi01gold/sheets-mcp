"""Tests run the real MCP server in memory against a fake Google client."""

from __future__ import annotations

import anyio
import pytest
from mcp import Client

from sheets_mcp import server

GRID = [
    ["Name", "Email", "Plan"],
    ["Ana", "ana@example.com", "Pro"],
    ["Beto", "beto@example.com", "Free"],
    ["Carla", "carla@example.com", "Pro"],
]


class FakeSheetsClient:
    def __init__(self) -> None:
        self.appended: list[list[str]] = []

    def read_range(self, spreadsheet_id: str, a1_range: str) -> list[list[str]]:
        return GRID

    def get_spreadsheet(self, spreadsheet_id: str) -> dict:
        return {
            "spreadsheet_id": spreadsheet_id,
            "title": "Customers",
            "url": "https://example.com",
            "sheets": [{"title": "Sheet1", "sheet_id": 0, "index": 0, "rows": 100, "columns": 26}],
        }

    def append_rows(self, spreadsheet_id, a1_range, values, value_input_option="USER_ENTERED"):
        self.appended.extend(values)
        return {"updates": {"updatedRange": "Sheet1!A5:C5", "updatedRows": len(values)}}


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    fake = FakeSheetsClient()
    monkeypatch.setattr(server, "_client", fake)
    return fake


def test_tools_are_registered():
    async def run():
        async with Client(server.mcp) as client:
            listing = await client.list_tools()
            return {tool.name for tool in listing.tools}

    names = anyio.run(run)
    assert {"list_spreadsheets", "get_spreadsheet", "read_range", "read_records"} <= names
    assert "append_rows" in names, "write tools should be registered when not read-only"


def test_read_records_labels_rows():
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(
                "read_records", {"spreadsheet_id": "abc", "sheet_name": "Sheet1"}
            )

    result = anyio.run(run)
    assert result.is_error is False
    data = result.structured_content
    assert data["headers"] == ["Name", "Email", "Plan"]
    assert data["total_records"] == 3
    assert data["records"][0]["Name"] == "Ana"
    assert data["records"][0]["_row"] == 2


def test_find_rows_matches_case_insensitively():
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(
                "find_rows",
                {
                    "spreadsheet_id": "abc",
                    "sheet_name": "Sheet1",
                    "column": "Plan",
                    "value": "pro",
                },
            )

    result = anyio.run(run)
    assert result.structured_content["match_count"] == 2


def test_unknown_column_returns_a_readable_error():
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(
                "find_rows",
                {
                    "spreadsheet_id": "abc",
                    "sheet_name": "Sheet1",
                    "column": "Nope",
                    "value": "x",
                },
            )

    result = anyio.run(run)
    assert result.is_error is True
    assert "not found" in result.content[0].text
