"""Thin wrapper around the Google Sheets and Drive REST APIs.

This module knows nothing about MCP. It loads credentials, calls Google, and
turns Google's ``HttpError`` into a readable :class:`SheetsError`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

READ_ONLY_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

READ_WRITE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DEFAULT_TOKEN_PATH = Path.home() / ".sheets-mcp" / "token.json"


class SheetsError(RuntimeError):
    """A failure the calling model can read and react to."""


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def read_only_mode() -> bool:
    """True when the server should expose read tools only."""
    return _bool_env("SHEETS_MCP_READ_ONLY", False)


def _token_path() -> Path:
    return Path(os.getenv("GOOGLE_OAUTH_TOKEN_FILE", str(DEFAULT_TOKEN_PATH))).expanduser()


def load_credentials(read_only: bool, *, allow_browser: bool = False):
    """Resolve credentials from the environment.

    Resolution order:

    1. ``GOOGLE_SERVICE_ACCOUNT_JSON`` — the service account key as raw JSON.
    2. ``GOOGLE_SERVICE_ACCOUNT_FILE`` — path to the service account key file.
    3. ``GOOGLE_OAUTH_CLIENT_FILE`` — OAuth client secrets, using a cached token.
    """
    scopes = READ_ONLY_SCOPES if read_only else READ_WRITE_SCOPES

    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        try:
            info = json.loads(raw_json)
        except json.JSONDecodeError as exc:  # pragma: no cover - config error
            raise SheetsError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}") from exc
        return service_account.Credentials.from_service_account_info(info, scopes=scopes)

    key_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if key_file:
        path = Path(key_file).expanduser()
        if not path.is_file():
            raise SheetsError(f"Service account file not found: {path}")
        return service_account.Credentials.from_service_account_file(str(path), scopes=scopes)

    client_file = os.getenv("GOOGLE_OAUTH_CLIENT_FILE")
    if client_file:
        return _oauth_credentials(Path(client_file).expanduser(), scopes, allow_browser)

    raise SheetsError(
        "No Google credentials configured. Set GOOGLE_SERVICE_ACCOUNT_FILE "
        "(recommended), GOOGLE_SERVICE_ACCOUNT_JSON, or GOOGLE_OAUTH_CLIENT_FILE."
    )


def _oauth_credentials(client_file: Path, scopes: list[str], allow_browser: bool):
    token_file = _token_path()
    creds: UserCredentials | None = None

    if token_file.is_file():
        creds = UserCredentials.from_authorized_user_file(str(token_file), scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds, token_file)
        return creds

    if not allow_browser:
        raise SheetsError(
            f"No cached OAuth token at {token_file}. Run `sheets-mcp auth` once in a "
            "terminal to authorize, then restart the MCP client."
        )

    if not client_file.is_file():
        raise SheetsError(f"OAuth client file not found: {client_file}")

    # Imported lazily: only the interactive `auth` command needs this dependency.
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), scopes)
    creds = flow.run_local_server(port=0)
    _save_token(creds, token_file)
    return creds


def _save_token(creds: UserCredentials, token_file: Path) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    token_file.chmod(0o600)


def _explain(exc: HttpError) -> str:
    status = getattr(exc.resp, "status", "?")
    try:
        detail = json.loads(exc.content.decode("utf-8"))["error"]["message"]
    except Exception:  # noqa: BLE001 - error bodies are not guaranteed
        detail = str(exc)
    if status == 403:
        detail += (
            " (If you are using a service account, share the spreadsheet with its "
            "client_email address first.)"
        )
    if status == 404:
        detail += " (Check the spreadsheet ID.)"
    return f"Google API error {status}: {detail}"


class SheetsClient:
    """Everything the MCP tools need, and nothing else."""

    def __init__(self, credentials) -> None:
        self._sheets = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        ).spreadsheets()
        self._drive = build("drive", "v3", credentials=credentials, cache_discovery=False)

    @classmethod
    def from_env(cls) -> SheetsClient:
        return cls(load_credentials(read_only_mode()))

    # -- reads ---------------------------------------------------------------

    def list_spreadsheets(self, name_contains: str | None = None, limit: int = 20) -> list[dict]:
        query = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
        if name_contains:
            escaped = name_contains.replace("'", "\\'")
            query += f" and name contains '{escaped}'"
        try:
            result = (
                self._drive.files()
                .list(
                    q=query,
                    pageSize=max(1, min(limit, 100)),
                    orderBy="modifiedTime desc",
                    fields="files(id,name,modifiedTime,webViewLink)",
                )
                .execute()
            )
        except HttpError as exc:
            raise SheetsError(_explain(exc)) from exc
        return result.get("files", [])

    def get_spreadsheet(self, spreadsheet_id: str) -> dict:
        try:
            data = self._sheets.get(
                spreadsheetId=spreadsheet_id,
                fields=(
                    "spreadsheetId,properties.title,spreadsheetUrl,"
                    "sheets.properties(sheetId,title,index,gridProperties)"
                ),
            ).execute()
        except HttpError as exc:
            raise SheetsError(_explain(exc)) from exc

        tabs = []
        for sheet in data.get("sheets", []):
            props = sheet["properties"]
            grid = props.get("gridProperties", {})
            tabs.append(
                {
                    "title": props["title"],
                    "sheet_id": props["sheetId"],
                    "index": props["index"],
                    "rows": grid.get("rowCount"),
                    "columns": grid.get("columnCount"),
                }
            )
        return {
            "spreadsheet_id": data["spreadsheetId"],
            "title": data["properties"]["title"],
            "url": data.get("spreadsheetUrl"),
            "sheets": tabs,
        }

    def read_range(self, spreadsheet_id: str, a1_range: str) -> list[list[str]]:
        try:
            result = self._sheets.values().get(
                spreadsheetId=spreadsheet_id,
                range=a1_range,
                valueRenderOption="FORMATTED_VALUE",
            ).execute()
        except HttpError as exc:
            raise SheetsError(_explain(exc)) from exc
        return result.get("values", [])

    # -- writes --------------------------------------------------------------

    def write_range(
        self,
        spreadsheet_id: str,
        a1_range: str,
        values: list[list[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> dict:
        try:
            return self._sheets.values().update(
                spreadsheetId=spreadsheet_id,
                range=a1_range,
                valueInputOption=value_input_option,
                body={"values": values},
            ).execute()
        except HttpError as exc:
            raise SheetsError(_explain(exc)) from exc

    def append_rows(
        self,
        spreadsheet_id: str,
        a1_range: str,
        values: list[list[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> dict:
        try:
            return self._sheets.values().append(
                spreadsheetId=spreadsheet_id,
                range=a1_range,
                valueInputOption=value_input_option,
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            ).execute()
        except HttpError as exc:
            raise SheetsError(_explain(exc)) from exc

    def clear_range(self, spreadsheet_id: str, a1_range: str) -> dict:
        try:
            return self._sheets.values().clear(
                spreadsheetId=spreadsheet_id, range=a1_range, body={}
            ).execute()
        except HttpError as exc:
            raise SheetsError(_explain(exc)) from exc

    def create_spreadsheet(self, title: str, sheet_titles: list[str] | None = None) -> dict:
        body: dict[str, Any] = {"properties": {"title": title}}
        if sheet_titles:
            body["sheets"] = [{"properties": {"title": t}} for t in sheet_titles]
        try:
            data = self._sheets.create(
                body=body, fields="spreadsheetId,spreadsheetUrl,properties.title"
            ).execute()
        except HttpError as exc:
            raise SheetsError(_explain(exc)) from exc
        return {
            "spreadsheet_id": data["spreadsheetId"],
            "title": data["properties"]["title"],
            "url": data.get("spreadsheetUrl"),
        }

    def add_sheet(self, spreadsheet_id: str, title: str) -> dict:
        try:
            data = self._sheets.batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
            ).execute()
        except HttpError as exc:
            raise SheetsError(_explain(exc)) from exc
        props = data["replies"][0]["addSheet"]["properties"]
        return {"title": props["title"], "sheet_id": props["sheetId"]}

    def share(self, file_id: str, email: str, role: str = "writer") -> dict:
        try:
            return self._drive.permissions().create(
                fileId=file_id,
                sendNotificationEmail=False,
                body={"type": "user", "role": role, "emailAddress": email},
                fields="id,role,emailAddress",
            ).execute()
        except HttpError as exc:
            raise SheetsError(_explain(exc)) from exc
