"""Typer-powered ShellRoom CLI."""

from __future__ import annotations

import asyncio
import getpass
from typing import Annotated

import typer

from shellroom import __version__
from shellroom.sdk import DEFAULT_SERVER_URL, ShellRoomClient, ShellRoomError
from shellroom.tui import ShellRoomApp

app = typer.Typer(
    no_args_is_help=True, 
    help="Ephemeral terminal-native group chat."
)


@app.command()
def create(
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="Display name to use in the room."),
    ] = None,
    server: Annotated[
        str,
        typer.Option("--server", "-s", help="ShellRoom server URL."),
    ] = DEFAULT_SERVER_URL,
) -> None:
    """Create a room and open the terminal chat UI."""

    asyncio.run(_create_room(name=name, server=server))


@app.command()
def join(
    room_id: Annotated[str, typer.Argument(help="Room ID to join.")],
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="Display name to use in the room."),
    ] = None,
    server: Annotated[
        str,
        typer.Option("--server", "-s", help="ShellRoom server URL."),
    ] = DEFAULT_SERVER_URL,
) -> None:
    """Join an existing room and open the terminal chat UI."""

    asyncio.run(_join_room(room_id=room_id, name=name, server=server))


@app.command()
def version() -> None:
    """Show the ShellRoom version."""

    typer.echo(__version__)


async def _create_room(name: str | None, server: str) -> None:
    display_name = _display_name(name)
    client = ShellRoomClient(server)

    try:
        created_room = await client.create_room(display_name=display_name)
        session = await client.connect(
            room_id=created_room.room_id,
            display_name=display_name,
            websocket_url=created_room.websocket_url,
        )
    except ShellRoomError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    await ShellRoomApp(
        session=session,
        invite_url=created_room.invite_url,
        join_command=created_room.join_command,
    ).run_async()


async def _join_room(room_id: str, name: str | None, server: str) -> None:
    display_name = _display_name(name)
    client = ShellRoomClient(server)

    try:
        session = await client.connect(room_id=room_id, display_name=display_name)
    except ShellRoomError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    await ShellRoomApp(
        session=session,
        invite_url=f"{client.server_url}/join/{room_id}",
        join_command=f"shellroom join {room_id}",
    ).run_async()


def _display_name(name: str | None) -> str:
    value = name.strip() if name is not None else getpass.getuser().strip()
    if not value:
        value = "Guest"
    return value[:24]


if __name__ == "__main__":
    app()
