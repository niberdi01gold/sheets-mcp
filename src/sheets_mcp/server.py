"""MCP server that exposes Google Sheets to any MCP client."""

from __future__ import annotations

import functools
import logging
import sys
from collections.abc import Callable
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .sheets import SheetsClient, SheetsError, load_credentials, read_only_mode

logger = logging.getLogger("sheets_mcp")

INSTRUCTIONS = """\
Tools for reading and editing Google Sheets.

Start with `list_spreadsheets` or `get_spreadsheet` to discover IDs and tab names
before reading. Prefer `read_records` over `read_range` when the sheet has a
header row: it returns labelled objects instead of a raw grid.

A spreadsheet ID is the long token in the URL:
https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit
"""

mcp = MCPServer("sheets-mcp", instructions=INSTRUCTIONS)

_client: SheetsClient | None = None


def client() -> SheetsClient:
    """Build the Google client on first use, so the server starts even if unconfigured."""
    global _client
    if _client is None:
        _client = SheetsClient.from_env()
    return _client


def surface_errors(fn: Callable) -> Callable:
    """Turn a SheetsError into a message the model can read and retry on."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except SheetsError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _records(rows: list[list[str]], header_row: int) -> tuple[list[str], list[dict]]:
    if len(rows) < header_row:
        return [], []
    headers = [str(h).strip() for h in rows[header_row - 1]]
    records = []
    for offset, row in enumerate(rows[header_row:], start=header_row + 1):
        record = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers) if h}
        record["_row"] = offset
        records.append(record)
    return headers, records


# --------------------------------------------------------------------------- #
# Read tools                                                                    #
# --------------------------------------------------------------------------- #


@mcp.tool()
@surface_errors
def list_spreadsheets(name_contains: str = "", limit: int = 20) -> dict[str, Any]:
    """List Google Sheets the credentials can access, most recently modified first.

    Args:
        name_contains: Optional case-insensitive substring to filter titles by.
        limit: Maximum number of spreadsheets to return (1-100).
    """
    files = client().list_spreadsheets(name_contains or None, limit)
    return {
        "count": len(files),
        "spreadsheets": [
            {
                "spreadsheet_id": f["id"],
                "title": f["name"],
                "modified": f.get("modifiedTime"),
                "url": f.get("webViewLink"),
            }
            for f in files
        ],
    }


@mcp.tool()
@surface_errors
def get_spreadsheet(spreadsheet_id: str) -> dict[str, Any]:
    """Get a spreadsheet's title, URL and the name and size of every tab in it.

    Call this before reading when you do not already know the tab names.

    Args:
        spreadsheet_id: The long token from the spreadsheet URL.
    """
    return client().get_spreadsheet(spreadsheet_id)


@mcp.tool()
@surface_errors
def read_range(spreadsheet_id: str, a1_range: str) -> dict[str, Any]:
    """Read a raw grid of cells from a spreadsheet.

    Args:
        spreadsheet_id: The long token from the spreadsheet URL.
        a1_range: A1 notation, e.g. "Sheet1", "Sheet1!A1:D50" or "Sheet1!A:A".
    """
    rows = client().read_range(spreadsheet_id, a1_range)
    return {"range": a1_range, "row_count": len(rows), "rows": rows}


@mcp.tool()
@surface_errors
def read_records(
    spreadsheet_id: str,
    sheet_name: str,
    header_row: int = 1,
    limit: int = 200,
) -> dict[str, Any]:
    """Read a tab as labelled records, using one row as the column headers.

    Each record includes a `_row` field with its 1-based sheet row number, which
    you can use to build an A1 range for a later update.

    Args:
        spreadsheet_id: The long token from the spreadsheet URL.
        sheet_name: The tab name, e.g. "Sales".
        header_row: 1-based row number holding the column headers.
        limit: Maximum number of data records to return.
    """
    rows = client().read_range(spreadsheet_id, sheet_name)
    headers, records = _records(rows, header_row)
    return {
        "sheet_name": sheet_name,
        "headers": headers,
        "total_records": len(records),
        "returned": min(len(records), limit),
        "records": records[:limit],
    }


@mcp.tool()
@surface_errors
def find_rows(
    spreadsheet_id: str,
    sheet_name: str,
    column: str,
    value: str,
    header_row: int = 1,
    exact: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Find records in a tab whose column matches a value.

    Args:
        spreadsheet_id: The long token from the spreadsheet URL.
        sheet_name: The tab name to search.
        column: Header name of the column to match against.
        value: Value to look for; matching is case-insensitive.
        header_row: 1-based row number holding the column headers.
        exact: True for an exact match, False for a substring match.
        limit: Maximum number of matches to return.
    """
    rows = client().read_range(spreadsheet_id, sheet_name)
    headers, records = _records(rows, header_row)
    if column not in headers:
        raise ToolError(f"Column {column!r} not found. Available columns: {headers}")

    needle = value.strip().lower()

    def hit(record: dict) -> bool:
        cell = str(record.get(column, "")).strip().lower()
        return cell == needle if exact else needle in cell

    matches = [r for r in records if hit(r)]

    return {
        "column": column,
        "value": value,
        "match_count": len(matches),
        "matches": matches[:limit],
    }


# --------------------------------------------------------------------------- #
# Write tools — not registered when SHEETS_MCP_READ_ONLY is set                 #
# --------------------------------------------------------------------------- #

if not read_only_mode():

    @mcp.tool()
    @surface_errors
    def write_range(
        spreadsheet_id: str,
        a1_range: str,
        values: list[list[str]],
        value_input_option: Literal["USER_ENTERED", "RAW"] = "USER_ENTERED",
    ) -> dict[str, Any]:
        """Overwrite a range of cells with the given rows. Existing values are replaced.

        Args:
            spreadsheet_id: The long token from the spreadsheet URL.
            a1_range: Top-left anchored range, e.g. "Sheet1!A2:C10".
            values: Rows of cell values, outer list is rows, inner list is columns.
            value_input_option: USER_ENTERED parses formulas and dates; RAW stores literally.
        """
        result = client().write_range(spreadsheet_id, a1_range, values, value_input_option)
        return {
            "updated_range": result.get("updatedRange"),
            "updated_rows": result.get("updatedRows", 0),
            "updated_cells": result.get("updatedCells", 0),
        }

    @mcp.tool()
    @surface_errors
    def append_rows(
        spreadsheet_id: str,
        sheet_name: str,
        values: list[list[str]],
        value_input_option: Literal["USER_ENTERED", "RAW"] = "USER_ENTERED",
    ) -> dict[str, Any]:
        """Append rows below the last used row of a tab. Nothing is overwritten.

        Args:
            spreadsheet_id: The long token from the spreadsheet URL.
            sheet_name: The tab to append to, e.g. "Sales".
            values: Rows of cell values, in the same column order as the sheet.
            value_input_option: USER_ENTERED parses formulas and dates; RAW stores literally.
        """
        result = client().append_rows(spreadsheet_id, sheet_name, values, value_input_option)
        updates = result.get("updates", {})
        return {
            "updated_range": updates.get("updatedRange"),
            "appended_rows": updates.get("updatedRows", 0),
        }

    @mcp.tool()
    @surface_errors
    def clear_range(spreadsheet_id: str, a1_range: str) -> dict[str, Any]:
        """Clear the values in a range, leaving formatting and the rows in place.

        Args:
            spreadsheet_id: The long token from the spreadsheet URL.
            a1_range: The range to clear, e.g. "Sheet1!A2:D100".
        """
        result = client().clear_range(spreadsheet_id, a1_range)
        return {"cleared_range": result.get("clearedRange", a1_range)}

    @mcp.tool()
    @surface_errors
    def create_spreadsheet(
        title: str,
        sheet_names: list[str] | None = None,
        share_with: str = "",
    ) -> dict[str, Any]:
        """Create a new spreadsheet and return its ID and URL.

        A spreadsheet created by a service account is owned by that account, so pass
        `share_with` to give a human account access to it.

        Args:
            title: Title of the new spreadsheet.
            sheet_names: Optional tab names to create instead of the default single tab.
            share_with: Optional email address to grant edit access to.
        """
        created = client().create_spreadsheet(title, sheet_names)
        if share_with:
            client().share(created["spreadsheet_id"], share_with)
            created["shared_with"] = share_with
        return created

    @mcp.tool()
    @surface_errors
    def add_sheet(spreadsheet_id: str, title: str) -> dict[str, Any]:
        """Add a new tab to an existing spreadsheet.

        Args:
            spreadsheet_id: The long token from the spreadsheet URL.
            title: Name of the new tab; must not already exist.
        """
        return client().add_sheet(spreadsheet_id, title)


# --------------------------------------------------------------------------- #
# Entry point                                                                   #
# --------------------------------------------------------------------------- #


def main() -> None:
    """Run the server over stdio, or `sheets-mcp auth` for the one-off OAuth flow."""
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        load_credentials(read_only_mode(), allow_browser=True)
        print("Authorized. Token cached; you can start the MCP server now.", file=sys.stderr)
        return
    mcp.run()


if __name__ == "__main__":
    main()
