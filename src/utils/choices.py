"""Shared app-command choice lists."""
from __future__ import annotations

from discord import app_commands

from .games import GAMES

GAME_CHOICES = [
    app_commands.Choice(name=g.label, value=g.key) for g in GAMES.values()
]
