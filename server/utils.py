"""Server utility helpers."""

from __future__ import annotations

import secrets

ROOM_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def generate_room_id(length: int = 8) -> str:
    """Generate a URL-safe room ID that avoids ambiguous characters."""
    return "".join(secrets.choice(ROOM_ID_ALPHABET) for _ in range(length))
