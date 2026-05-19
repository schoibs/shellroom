"""Textual terminal UI for a ShellRoom chat session."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input, Static

from shellroom.sdk import RoomSession


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

    #message-input {
        dock: bottom;
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"Room {self.session.room_id} | Invite: {self.invite_url} | {self.join_command}",
            id="room-meta",
        )
        yield Static("Connected", id="connection-status")
        yield VerticalScroll(id="message-log")
        yield Input(placeholder="Type a message and press Enter", id="message-input")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = f"ShellRoom: {self.session.room_id}"
        self.sub_title = self.session.display_name
        self._receiver_task = asyncio.create_task(self._receive_events())
        self.query_one("#message-input", Input).focus()

    async def on_unmount(self) -> None:
        if self._receiver_task is not None:
            self._receiver_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._receiver_task
        await self.session.close()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.rstrip("\r\n")
        event.input.value = ""

        if not text.strip():
            return

        try:
            await self.session.send_chat(text)
        except Exception as exc:
            await self._append_line(f"Could not send message: {exc}", "error")
            self._set_status("Disconnected", is_error=True)

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
            display_name = _display_name(payload)
            await self._append_line(f"{display_name} joined", "system")
        elif event_type == "user_left":
            display_name = _display_name(payload)
            await self._append_line(f"{display_name} left", "system")
        elif event_type == "error":
            code = str(payload.get("code") or "ERROR")
            message = str(payload.get("message") or "Unknown error.")
            await self._append_line(f"{code}: {message}", "error")
        else:
            await self._append_line(f"Unsupported event: {event_type}", "error")

    async def _handle_room_state(self, payload: dict[str, Any]) -> None:
        users = payload.get("users")
        if isinstance(users, list):
            names = [
                str(user.get("display_name"))
                for user in users
                if isinstance(user, dict) and user.get("display_name")
            ]
            if names:
                await self._append_line(
                    f"Online: {', '.join(escape(name) for name in names)}",
                    "system",
                )

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
