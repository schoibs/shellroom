"""Textual terminal UI for a ShellRoom chat session."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input, Static

from shellroom.sdk import RoomSession


TYPING_TIMEOUT_SECONDS = 3.0

# the current participant known by the TUI
@dataclass(slots=True)
class RosterUser:
    client_id: str
    display_name: str
    status: str = "online"
    typing: bool = False


class ShellRoomApp(App[None]):
    """Minimal Textual app for live ShellRoom chat."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #room-meta {
        height: auto;
        padding: 0 1;
        background: $surface;
        color: $text;
    }

    #connection-status {
        height: 1;
        padding: 0 1;
        color: $success;
    }

    #message-log {
        height: 1fr;
        border: solid $accent;
        padding: 0 1;
        overflow-y: auto;
    }

    #typing-indicator {
        height: 1;
        padding: 0 1;
        color: $warning;
    }

    #message-input {
        height: 3;
    }

    .system {
        color: $text-muted;
    }

    .error {
        color: $error;
    }

    .mine {
        color: $success;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("escape", "quit", "Quit"),
    ]

    def __init__(
        self,
        session: RoomSession,
        invite_url: str,
        join_command: str,
    ) -> None:
        super().__init__()
        self.session = session
        self.invite_url = invite_url
        self.join_command = join_command
        self._receiver_task: asyncio.Task[None] | None = None
        self._typing_timeout_task: asyncio.Task[None] | None = None
        self._typing_started = False
        self._users: dict[str, RosterUser] = {
            session.client_id: RosterUser(
                client_id=session.client_id,
                display_name=session.display_name,
            )
        }

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"Room {self.session.room_id} | Invite: {self.invite_url} | {self.join_command}",
            id="room-meta",
        )
        yield Static("Connected", id="connection-status")
        yield VerticalScroll(id="message-log")
        yield Static("", id="typing-indicator")
        yield Input(placeholder="Type a message and press Enter", id="message-input")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = f"ShellRoom: {self.session.room_id}"
        self.sub_title = self.session.display_name
        self._receiver_task = asyncio.create_task(self._receive_events())
        self._refresh_status_line()
        self.query_one("#message-input", Input).focus()

    async def on_unmount(self) -> None:
        if self._typing_started:
            with suppress(Exception):
                await self.session.send_typing_stop()
            self._typing_started = False
        self._cancel_typing_timeout()

        if self._receiver_task is not None:
            self._receiver_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._receiver_task
        await self.session.close()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.rstrip("\r\n")
        event.input.value = ""
        await self._stop_typing_if_needed()

        if not text.strip():
            return

        if text.lstrip().startswith("/"):
            await self._handle_slash_command(text.strip())
            return

        try:
            await self.session.send_chat(text)
        except Exception as exc:
            await self._append_line(f"Could not send message: {exc}", "error")
            self._set_status("Disconnected", is_error=True)

    async def on_input_changed(self, event: Input.Changed) -> None:
        text = event.value
        is_normal_message = bool(text.strip()) and not text.lstrip().startswith("/")

        if is_normal_message:
            await self._refresh_typing()
            self._arm_typing_timeout()
        else:
            await self._stop_typing_if_needed()

    async def _receive_events(self) -> None:
        try:
            async for event in self.session.events():
                await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._append_line(f"Connection closed: {exc}", "error")
            self._set_status("Disconnected", is_error=True)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        if event_type == "room_state":
            await self._handle_room_state(payload)
        elif event_type == "chat_message":
            await self._handle_chat_message(payload)
        elif event_type == "user_joined":
            self._upsert_user(payload, default_status="online")
            self._refresh_status_line()
            display_name = _display_name(payload)
            await self._append_line(f"{escape(display_name)} joined", "system")
        elif event_type == "user_left":
            self._remove_user(payload)
            self._refresh_status_line()
            self._update_typing_indicator()
            display_name = _display_name(payload)
            await self._append_line(f"{escape(display_name)} left", "system")
        elif event_type in {"user_idle", "user_active"}:
            self._upsert_user(payload, default_status="online")
            self._refresh_status_line()
        elif event_type == "typing_users":
            self._handle_typing_users(payload)
        elif event_type == "error":
            code = str(payload.get("code") or "ERROR")
            message = str(payload.get("message") or "Unknown error.")
            await self._append_line(f"{code}: {message}", "error")
        else:
            await self._append_line(f"Unsupported event: {event_type}", "error")

    async def _handle_room_state(self, payload: dict[str, Any]) -> None:
        users = payload.get("users")
        if isinstance(users, list):
            self._users.clear()
            for user in users:
                if isinstance(user, dict):
                    self._upsert_user(user, default_status="online")
            if self.session.client_id not in self._users:
                self._users[self.session.client_id] = RosterUser(
                    client_id=self.session.client_id,
                    display_name=self.session.display_name,
                )

            names = [user.display_name for user in self._users.values()]
            if names:
                await self._append_line(
                    f"Online: {', '.join(escape(name) for name in names)}",
                    "system",
                )
            self._refresh_status_line()

        messages = payload.get("recent_messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict):
                    await self._handle_chat_message(message)

    async def _handle_chat_message(self, payload: dict[str, Any]) -> None:
        display_name = _display_name(payload)
        text = str(payload.get("text") or "")
        classes = "mine" if payload.get("client_id") == self.session.client_id else ""
        await self._append_line(f"{escape(display_name)}: {escape(text)}", classes)

    async def _handle_slash_command(self, text: str) -> None:
        command = text.split(maxsplit=1)[0].lower()
        if command == "/who":
            await self._show_who()
            return

        await self._append_line(f"Unknown command: {escape(command)}", "error")

    async def _show_who(self) -> None:
        if not self._users:
            await self._append_line("No users are visible yet.", "system")
            return

        entries = [
            _format_roster_user(user, self.session.client_id)
            for user in self._users.values()
        ]
        await self._append_line(f"In room: {', '.join(entries)}", "system")

    async def _refresh_typing(self) -> None:
        try:
            await self.session.send_typing_start()
        except Exception as exc:
            await self._append_line(f"Could not send typing status: {exc}", "error")
            self._set_status("Disconnected", is_error=True)
            return

        self._typing_started = True
        self._set_local_typing(True)

    async def _stop_typing_if_needed(self) -> None:
        self._cancel_typing_timeout()
        if not self._typing_started:
            return

        self._typing_started = False
        self._set_local_typing(False)

        try:
            await self.session.send_typing_stop()
        except Exception as exc:
            await self._append_line(f"Could not send typing status: {exc}", "error")
            self._set_status("Disconnected", is_error=True)

    def _arm_typing_timeout(self) -> None:
        self._cancel_typing_timeout()
        self._typing_timeout_task = asyncio.create_task(self._typing_timeout())

    def _cancel_typing_timeout(self) -> None:
        if self._typing_timeout_task is not None:
            self._typing_timeout_task.cancel()
            self._typing_timeout_task = None

    async def _typing_timeout(self) -> None:
        try:
            await asyncio.sleep(TYPING_TIMEOUT_SECONDS)
            self._typing_timeout_task = None
            await self._stop_typing_if_needed()
        except asyncio.CancelledError:
            raise

    def _upsert_user(
        self,
        payload: dict[str, Any],
        default_status: str,
    ) -> RosterUser | None:
        client_id = _client_id(payload)
        if client_id is None:
            return None

        status = _status(payload, default_status)
        user = self._users.get(client_id)
        if user is None:
            user = RosterUser(
                client_id=client_id,
                display_name=_display_name(payload),
                status=status,
            )
            self._users[client_id] = user
        else:
            user.display_name = _display_name(payload)
            user.status = status
        return user

    def _remove_user(self, payload: dict[str, Any]) -> None:
        client_id = _client_id(payload)
        if client_id is not None:
            self._users.pop(client_id, None)

    def _handle_typing_users(self, payload: dict[str, Any]) -> None:
        for user in self._users.values():
            user.typing = False

        client_ids = payload.get("client_ids")
        if isinstance(client_ids, list):
            for client_id in client_ids:
                if isinstance(client_id, str) and client_id in self._users:
                    self._users[client_id].typing = True
        else:
            raw_names = payload.get("users")
            names = (
                {name for name in raw_names if isinstance(name, str)}
                if isinstance(raw_names, list)
                else set()
            )
            for user in self._users.values():
                user.typing = user.display_name in names

        self._update_typing_indicator()

    def _set_local_typing(self, is_typing: bool) -> None:
        user = self._users.get(self.session.client_id)
        if user is not None:
            user.typing = is_typing
        self._update_typing_indicator()

    def _update_typing_indicator(self) -> None:
        names = [
            user.display_name
            for user in self._users.values()
            if user.typing and user.client_id != self.session.client_id
        ]
        self.query_one("#typing-indicator", Static).update(_typing_sentence(names))

    def _refresh_status_line(self) -> None:
        user_count = len(self._users)
        noun = "user" if user_count == 1 else "users"
        self._set_status(f"Connected | {user_count} {noun}")

    async def _append_line(self, line: str, classes: str = "") -> None:
        message_log = self.query_one("#message-log", VerticalScroll)
        await message_log.mount(Static(line, classes=classes))
        message_log.scroll_end(animate=False)

    def _set_status(self, text: str, is_error: bool = False) -> None:
        status = self.query_one("#connection-status", Static)
        status.update(text)
        status.set_class(is_error, "error")


def _display_name(payload: dict[str, Any]) -> str:
    value = payload.get("display_name")
    if isinstance(value, str) and value:
        return value
    return "Someone"


def _client_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("client_id")
    if isinstance(value, str) and value:
        return value
    return None


def _status(payload: dict[str, Any], default: str) -> str:
    value = payload.get("status")
    if value in {"online", "idle"}:
        return str(value)
    return default


def _format_roster_user(user: RosterUser, local_client_id: str) -> str:
    labels: list[str] = []
    if user.client_id == local_client_id:
        labels.append("you")
    if user.status == "idle":
        labels.append("idle")
    if user.typing:
        labels.append("typing")

    suffix = f" ({', '.join(labels)})" if labels else ""
    return f"{escape(user.display_name)}{suffix}"


def _typing_sentence(names: list[str]) -> str:
    if not names:
        return ""

    escaped_names = [escape(name) for name in names]
    if len(escaped_names) == 1:
        return f"{escaped_names[0]} is typing..."
    if len(escaped_names) == 2:
        return f"{escaped_names[0]} and {escaped_names[1]} are typing..."

    others = len(escaped_names) - 2
    noun = "other" if others == 1 else "others"
    return f"{escaped_names[0]}, {escaped_names[1]}, and {others} {noun} are typing..."
