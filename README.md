# sheets-mcp

An [MCP](https://modelcontextprotocol.io) server that connects Claude — or any MCP client — to Google Sheets.

Ask in plain language, and the model reads and edits your spreadsheets directly:

> "In my *Q3 Pipeline* sheet, find every deal marked `Negotiation` and add a follow-up row to the *Tasks* tab for each one."

Built on the official [MCP Python SDK](https://py.sdk.modelcontextprotocol.io) (v2) and the Google Sheets API v4.

---

## Why this exists

Most spreadsheet automation dies at the glue layer: a script that reads a sheet is easy, but a script that reads *whatever sheet someone asks about* is not. MCP moves that decision to the model. You expose a handful of well-described tools, and the client figures out which to call.

Two design choices worth knowing about:

- **`read_records` over raw grids.** Handing a model a 2D array of strings wastes tokens and invites off-by-one errors. `read_records` uses the header row to return labelled objects, each tagged with its real sheet row number so a follow-up write lands in the right place.
- **Read-only mode is enforced by registration, not by a check.** With `SHEETS_MCP_READ_ONLY=true`, the write tools are never registered, so the model cannot see them and cannot be talked into calling them.

---

## Tools

| Tool | What it does |
| --- | --- |
| `list_spreadsheets` | Lists accessible spreadsheets, newest first, optionally filtered by title. |
| `get_spreadsheet` | Returns a spreadsheet's title, URL, and every tab with its dimensions. |
| `read_range` | Reads a raw grid from an A1 range. |
| `read_records` | Reads a tab as labelled records using a header row. Each record carries `_row`. |
| `find_rows` | Returns records whose named column matches a value (exact or substring). |
| `write_range` | Overwrites a range with the rows you supply. |
| `append_rows` | Appends rows below the last used row. Overwrites nothing. |
| `clear_range` | Clears values in a range, leaving formatting intact. |
| `create_spreadsheet` | Creates a spreadsheet, optionally with named tabs, optionally shared with an email. |
| `add_sheet` | Adds a tab to an existing spreadsheet. |

The last five are omitted entirely in read-only mode.

---

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/niberdi01gold/sheets-mcp.git
cd sheets-mcp
pip install -e .
```

---

## Google setup (5 minutes)

A service account is the recommended path: no browser flow, no token to refresh, and it only ever sees the sheets you explicitly share with it.

1. In the [Google Cloud Console](https://console.cloud.google.com), create a project.
2. Enable the **Google Sheets API** and the **Google Drive API**.
3. Go to **IAM & Admin → Service Accounts**, create one, then **Keys → Add key → JSON**. Save the file somewhere outside the repo.
4. Open the JSON and copy the `client_email` value.
5. In Google Sheets, press **Share** on each spreadsheet you want the server to reach and paste that address (Viewer for read-only, Editor for writes).

Step 5 is the one people miss. A service account starts with access to nothing; sharing is what grants it.

Then point the server at the key:

```bash
cp .env.example .env
# set GOOGLE_SERVICE_ACCOUNT_FILE=/absolute/path/to/service-account.json
```

<details>
<summary>Alternative: OAuth as your own Google account</summary>

Use this when you want the server to see everything *you* can see, without sharing sheets one by one. Create an OAuth client of type **Desktop app**, download the JSON, then:

```bash
export GOOGLE_OAUTH_CLIENT_FILE=/absolute/path/to/oauth-client.json
sheets-mcp auth      # opens a browser once; caches a token at ~/.sheets-mcp/token.json
```

The server itself never opens a browser — it reads the cached token and fails with a clear message if there isn't one.
</details>
---

## Connect it to a client

### Claude Desktop

Edit `claude_desktop_config.json` (**Settings → Developer → Edit Config**):

```json
{
  "mcpServers": {
    "sheets": {
      "command": "sheets-mcp",
      "env": {
        "GOOGLE_SERVICE_ACCOUNT_FILE": "/absolute/path/to/service-account.json",
        "SHEETS_MCP_READ_ONLY": "false"
      }
    }
  }
}
```

Use absolute paths. Claude Desktop launches the server in its own process and does not inherit your shell environment.

### Claude Code

```bash
claude mcp add sheets -e GOOGLE_SERVICE_ACCOUNT_FILE=/absolute/path/to/service-account.json -- sheets-mcp
```

### MCP Inspector (for development)

```bash
mcp dev src/sheets_mcp/server.py
```

---

## Usage examples

Once connected, these are the kinds of requests the tools are shaped for.

**Read and summarise**

> Open the spreadsheet called "Monthly Expenses" and tell me which category grew the most between June and July.

The model calls `list_spreadsheets` → `get_spreadsheet` → `read_records`, then reasons over the labelled rows.

**Append without clobbering**

> Add these three leads to the Leads tab: Acme (acme@example.com, cold), Globex (ops@globex.com, warm), Initech (hi@initech.com, cold).

Calls `append_rows`, which anchors below the last used row, so concurrent edits by a human do not get overwritten.

**Targeted update**

> In the Inventory tab, find the row where SKU is TH-4410 and set its Stock column to 0.

Calls `find_rows` to get the record and its `_row`, then `write_range` on the single cell.

**Build a new sheet from a conversation**

> Create a spreadsheet called "Q4 Content Calendar" with tabs for Blog, Email and Social, share it with me at nico@example.com, and fill the Blog tab with a header row.

Calls `create_spreadsheet` then `write_range`.

---

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | — | Path to the service account JSON key. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | — | The same key inlined, for hosts without a filesystem. |
| `GOOGLE_OAUTH_CLIENT_FILE` | — | OAuth client secrets, for the user-account flow. |
| `GOOGLE_OAUTH_TOKEN_FILE` | `~/.sheets-mcp/token.json` | Where the cached OAuth token lives. |
| `SHEETS_MCP_READ_ONLY` | `false` | When true, only the read tools are registered. |

Credentials are resolved in the order listed above; the first one set wins.

---

## Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
```

The tests run the real server in memory through the SDK's `Client`, with a fake Google client injected — no network, no credentials required.

---

## Notes and limits

- Reads use `FORMATTED_VALUE`, so you get what a human sees in the cell, not the underlying float.
- `create_spreadsheet` under a service account produces a file owned by that account. Pass `share_with` or it will be invisible in your Drive.
- Cell formatting, charts, and pivot tables are out of scope for now. `batchUpdate` support is the obvious next addition.
- Very large tabs are read whole before slicing. Fine to a few thousand rows; range-limited reads would be the fix beyond that.

---

## License

MIT — see [LICENSE](LICENSE).
