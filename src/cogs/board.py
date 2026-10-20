"""The event board: one pinned message per subscription, edited in place.

Replaces the per-day digest. See :mod:`src.utils.boardcard` for why a day was
the wrong unit; in short, which day a match belonged to depended on a handover
hour, and an event that plays across that hour had its schedule cut in half.

The board is posted once, pinned, and then edited every sweep for as long as
the event has fixtures. Three things follow from it being *one* message:

* **It can't show per-member state.** Discord renders one body for every
  viewer, so "you picked Gen.G" is impossible here. The buttons answer
  ephemerally and the picker shows a member their own picks.
* **Editing has to be cheap.** The sweep compares a digest of what it would
  render against what it last rendered and does nothing when they match, so a
  quiet five minutes costs no API call.
* **It can't carry every match as a button.** Discord allows five rows of five,
  so the four matches nearest kick-off get a row each — one tap, as before —
  and everything else is reachable through a picker.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..services.pandascore import PandaScoreError
from ..utils.boardcard import (
    CONTROLS_FIT_AT, MAX_TAPPABLE, TAPPABLE_PER_PART, build_board_parts,
    button_slices, open_matches, signature,
)
from ..utils.conviction import as_view, pick_panel
from ..utils.games import resolve_slug
from ..utils.guildprefs import digest_tz
from ..utils.matches import league_id as match_league_id
from ..utils.matches import opponents
from ..utils.matches import tournament_id as match_tournament_id
from ..utils.predictions import submit_prediction
from ..utils.scoring import round_tag
from ..utils.subscriptions import wants_votes
from ..utils.tiers import filter_for
from ..utils.tournaments import parse_dt

log = logging.getLogger("aurorabot.cogs.board")

SWEEP_MINUTES = 5
# How far ahead a board looks. Long enough to cover an event's week, short
# enough that it isn't advertising fixtures nobody can plan around yet.
HORIZON = timedelta(days=7)
# Matches offered in the picker. Discord's select caps at 25 options.
MAX_PICKER = 25
UTC = timezone.utc


def _boardable(sub) -> bool:
    """Subscriptions that get a board: a league or a single stage.

    Same rule as the digest had. A team plays once a day and a whole-game
    board would be a wall of text.
    """
    scope = sub["scope"] or "game"
    if scope == "league":
        return sub["league_id"] is not None
    if scope == "tournament":
        return sub["tournament_id"] is not None
    return False


def _target_id(sub) -> int | None:
    if (sub["scope"] or "game") == "league":
        return int(sub["league_id"]) if sub["league_id"] is not None else None
    return int(sub["tournament_id"]) if sub["tournament_id"] is not None else None


def _target_name(sub) -> str | None:
    if (sub["scope"] or "game") == "league":
        return sub["league_name"]
    return sub["tournament_name"]


def _label(match: dict, tz) -> str:
    """``04:00 Gen.G`` — the time disambiguates a team playing twice a day."""
    begin = parse_dt(match.get("begin_at"))
    return f"{begin.astimezone(tz):%H:%M} " if begin else ""


def _span(first: dict, last: dict, tz) -> str:
    """`` · 09:00–11:00`` for a bank of buttons, or nothing if unknown."""
    a = parse_dt(first.get("begin_at"))
    b = parse_dt(last.get("begin_at"))
    if a is None:
        return ""
    if b is None or a == b:
        return f" · {a.astimezone(tz):%H:%M}"
    return f" · {a.astimezone(tz):%H:%M}–{b.astimezone(tz):%H:%M}"


class BoardVoteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"aurora:board:(?P<board_id>[0-9]+):(?P<match_id>[0-9]+):(?P<side>[01])",
):
    """One team's button on an event board.

    The board outlives any single day, so a click can arrive long after the
    message was first posted and after any number of restarts. Everything
    needed is either in the custom_id or read back from ``board_matches`` —
    never from the label, so a renamed team can't corrupt a pick.
    """

    def __init__(
        self,
        board_id: int,
        match_id: int,
        side: int,
        *,
        label: str = "…",
        emoji=None,
        row: int | None = None,
    ) -> None:
        self.board_id = board_id
        self.match_id = match_id
        self.side = side
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"aurora:board:{board_id}:{match_id}:{side}",
                emoji=emoji,
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["board_id"]), int(match["match_id"]),
                   int(match["side"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _vote(interaction, self.board_id, self.match_id, self.side)


async def _vote(interaction: discord.Interaction, board_id: int,
                match_id: int, side: int) -> None:
    """Record a pick made from a board, and reply only to the clicker."""
    db = interaction.client.db
    row = await db.board_match(board_id, match_id)
    if row is None:
        await interaction.response.send_message(
            "That match isn't on the board any more.", ephemeral=True
        )
        return

    begin = parse_dt(row["begin_at"])
    if begin is not None and begin <= datetime.now(UTC):
        # The board edits on a timer, so a button can be a few minutes stale.
        await interaction.response.send_message(
            "That one has already started — predictions closed at kick-off.",
            ephemeral=True,
        )
        return

    team_a = {"id": int(row["team_a_id"]), "name": row["team_a_name"]}
    team_b = {"id": int(row["team_b_id"]), "name": row["team_b_name"]}
    team, opponent = (team_a, team_b) if side == 0 else (team_b, team_a)

    tour_id, tour_name = row["tournament_id"], row["tournament_name"]
    if tour_id is None:
        meta = await db.board_meta(board_id)
        tour_id = meta["tournament_id"] if meta else None
        tour_name = meta["tournament_name"] if meta else None

    _, message = await submit_prediction(
        db,
        user_id=interaction.user.id,
        display_name=interaction.user.display_name,
        match_id=match_id,
        game=None,
        team=team,
        opponent=opponent,
        begin_at=row["begin_at"],
        guild_id=interaction.guild_id,
        tournament_id=tour_id,
        tournament_name=tour_name,
    )
    panel = await pick_panel(db, interaction.user.id, match_id)
    await interaction.response.send_message(
        message, view=as_view(panel), ephemeral=True
    )


class PickerSelect(discord.ui.Select):
    """The rest of the week, as a menu. Ephemeral, so it holds no shared state."""

    def __init__(self, board_id: int, rows, tz, mine: dict) -> None:
        self.board_id = board_id
        options = []
        for row in rows[:MAX_PICKER]:
            begin = parse_dt(row["begin_at"])
            when = f"{begin.astimezone(tz):%a %H:%M}" if begin else "time TBD"
            pick = mine.get(int(row["match_id"]))
            label = f"{row['team_a_name']} vs {row['team_b_name']}"
            description = when
            if pick is not None:
                description = f"{when} · you picked {pick['predicted_team_name']}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(int(row["match_id"])),
                    description=description[:100],
                    emoji="✅" if pick is not None else None,
                )
            )
        super().__init__(
            placeholder="Pick a match…",
            options=options or [discord.SelectOption(label="Nothing open",
                                                     value="none")],
            disabled=not options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        match_id = int(self.values[0])
        row = await interaction.client.db.board_match(self.board_id, match_id)
        if row is None:
            await interaction.response.send_message(
                "That match isn't on the board any more.", ephemeral=True
            )
            return
        view = discord.ui.View(timeout=180)
        for side, name in ((0, row["team_a_name"]), (1, row["team_b_name"])):
            view.add_item(
                BoardVoteButton(self.board_id, match_id, side, label=name)
            )
        await interaction.response.send_message(
            f"**{row['team_a_name']}** vs **{row['team_b_name']}** — who wins?",
            view=view, ephemeral=True,
        )


class PickerButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"aurora:boardpick:(?P<board_id>[0-9]+)",
):
    """Opens the week's matches as a private menu."""

    def __init__(self, board_id: int, label: str = "🎲 More matches") -> None:
        self.board_id = board_id
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.secondary,
                custom_id=f"aurora:boardpick:{board_id}",
                row=4,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["board_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        rows = [
            r for r in await db.board_match_rows(self.board_id)
            if (parse_dt(r["begin_at"]) or datetime.now(UTC)) > datetime.now(UTC)
        ]
        if not rows:
            await interaction.response.send_message(
                "Nothing on this board is still open — every fixture has "
                "started or finished.", ephemeral=True
            )
            return
        mine = await db.user_picks_for_matches(
            interaction.user.id, interaction.guild_id,
            [int(r["match_id"]) for r in rows],
        )
        tz = digest_tz(interaction.client.settings,
                       await db.guild_settings(interaction.guild_id))
        view = discord.ui.View(timeout=180)
        view.add_item(PickerSelect(self.board_id, rows, tz, mine))
        done = len(mine)
        note = (
            f"**{len(rows)}** still open"
            + (f" · you've picked **{done}**" if done else "")
        )
        await interaction.response.send_message(note, view=view, ephemeral=True)


class MyPicksButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"aurora:boardmine:(?P<board_id>[0-9]+)",
):
    """A member's own picks on this event — the half a shared card can't show."""

    def __init__(self, board_id: int) -> None:
        self.board_id = board_id
        super().__init__(
            discord.ui.Button(
                label="📋 My picks",
                style=discord.ButtonStyle.secondary,
                custom_id=f"aurora:boardmine:{board_id}",
                row=4,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["board_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        rows = await db.board_match_rows(self.board_id)
        mine = await db.user_picks_for_matches(
            interaction.user.id, interaction.guild_id,
            [int(r["match_id"]) for r in rows],
        )
        if not mine:
            await interaction.response.send_message(
                "You haven't picked anything on this board yet — tap a team, "
                "or use **🎲 More matches** for the rest of the week.",
                ephemeral=True,
            )
            return
        tz = digest_tz(interaction.client.settings,
                       await db.guild_settings(interaction.guild_id))
        lines = []
        for row in rows:
            pick = mine.get(int(row["match_id"]))
            if pick is None:
                continue
            begin = parse_dt(row["begin_at"])
            when = f"`{begin.astimezone(tz):%a %H:%M}`" if begin else "`  ?  `"
            against = (row["team_b_name"]
                       if int(pick["predicted_team_id"]) == int(row["team_a_id"])
                       else row["team_a_name"])
            mark = {"won": "✅", "lost": "❌", "void": "➖"}.get(pick["status"], "⏳")
            token = " 💥" if pick["doubled"] else ""
            lines.append(
                f"{mark} {when} **{pick['predicted_team_name']}** "
                f"over {against}{token}"
            )
        await interaction.response.send_message(
            "\n".join(lines)[:1900], ephemeral=True
        )


class Board(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.sweep.start()

    async def cog_unload(self) -> None:
        self.sweep.cancel()

    @tasks.loop(minutes=SWEEP_MINUTES)
    async def sweep(self) -> None:
        try:
            await self._sweep_once()
        except Exception:  # noqa: BLE001 - the loop must outlive failures
            log.exception("board sweep failed")

    @sweep.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="board", description="Post or refresh this channel's event board."
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def board(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        subs = [
            s for s in await self.bot.db.list_subscriptions(interaction.guild_id)
            if _boardable(s) and int(s["channel_id"]) == interaction.channel_id
        ]
        if not subs:
            await interaction.followup.send(
                "This channel follows no league or tournament. Add one with "
                "`/alerts add game:… tournament:…`."
            )
            return
        done = 0
        for sub in subs:
            if await self._refresh(sub):
                done += 1
        await interaction.followup.send(
            f"Board up to date for {done} subscription(s)." if done
            else "Nothing scheduled in those tournaments yet."
        )

    async def _sweep_once(self) -> None:
        subs = [s for s in await self.bot.db.all_subscriptions() if _boardable(s)]
        if not subs:
            return
        enabled = await self.bot.db.all_enabled_games()
        for sub in subs:
            if sub["game"] not in enabled.get(str(sub["guild_id"]), frozenset()):
                continue
            try:
                await self._refresh(sub)
            except PandaScoreError as exc:
                log.debug("board: %s lookup failed: %s", sub["game"], exc)
            except Exception:  # noqa: BLE001 - one board must not stop the rest
                log.exception("board: could not refresh subscription %s", sub["id"])

    async def _matches(self, sub) -> list[dict]:
        """Everything in scope inside the horizon, drawn or not."""
        slug = resolve_slug(sub["game"], self.bot.settings.cs_slug)
        horizon = datetime.now(UTC) + HORIZON
        feed = await self.bot.api.upcoming_until(slug, horizon)
        feed += await self.bot.api.running_all(slug)

        by_league = (sub["scope"] or "game") == "league"
        wanted = int(sub["league_id"] if by_league else sub["tournament_id"])
        out: dict[int, dict] = {}
        for match in filter_for(self.bot.settings, feed):
            got = (match_league_id(match) if by_league
                   else match_tournament_id(match))
            if got != wanted:
                continue
            begin = parse_dt(match.get("begin_at"))
            if begin is None or begin > horizon:
                continue
            out[int(match["id"])] = match
        return list(out.values())

    async def _refresh(self, sub) -> bool:
        """Post, edit or retire one board. True if it now exists and is current."""
        channel_id = int(sub["channel_id"])
        guild_id = int(sub["guild_id"]) if sub["guild_id"] else None
        board = await self.bot.db.board_for(channel_id, int(sub["id"]))

        matches = await self._matches(sub)
        if not matches and board is None:
            return False            # nothing on, nothing posted: say nothing

        tz = digest_tz(self.bot.settings,
                       await self.bot.db.guild_settings(guild_id))
        live = open_matches(matches)
        backers = await self.bot.db.picks_for_matches(
            guild_id, [int(m["id"]) for m in matches]
        )
        # One bank of buttons per message, so eight games on a Saturday all get
        # a button rather than being hidden behind a dropdown. With predictions
        # off there are no buttons to run out of, so there is nothing to split
        # for and the schedule stays in one message unless the text overflows.
        votes = wants_votes(sub)
        banks = button_slices(matches) if votes else [[]]
        notes = [None]
        for bank in banks[1:]:
            first, last = bank[0], bank[-1]
            span = _span(first, last, tz)
            notes.append(f"🎲 The rest of the day{span}")
        parts = build_board_parts(
            self.bot, matches, title=_target_name(sub) or "Event",
            tz=tz, backers=backers, min_parts=len(banks), notes=notes,
        )
        # A bank with no message to sit on would be a silently unpickable
        # match, so never offer more banks than there are parts.
        banks = banks[:len(parts)]
        sig = signature(parts, [int(m["id"]) for m in live])

        if board is None:
            board_id = await self.bot.db.create_board(
                guild_id=guild_id, channel_id=channel_id,
                subscription_id=int(sub["id"]),
                tournament_id=_target_id(sub), tournament_name=_target_name(sub),
                game=sub["game"],
            )
            if board_id is None:
                return False        # another sweep claimed it
        else:
            board_id = int(board["id"])
            if board["signature"] == sig and board["message_id"]:
                return True         # nothing has changed; no API call

        await self._store(board_id, live)

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            log.warning("board: channel %s is not visible", channel_id)
            return False

        for index, part in enumerate(parts):
            bank = banks[index] if index < len(banks) else []
            # The last part carries the controls, and the overflow picker only
            # if there are more open matches than two messages can hold.
            part_view = self._view(
                board_id, bank, tz,
                controls=(index == len(parts) - 1),
                overflow=len(live) > MAX_TAPPABLE and index == len(parts) - 1,
            ) if votes else None
            if not await self._place(channel, board, board_id, index + 1,
                                     part, part_view):
                return False

        if len(parts) == 1 and board is not None and board["message_id_2"]:
            # The week shrank back to one card. Retire the spare rather than
            # leaving a stale half-schedule pinned under the live one.
            await self._retire(channel, int(board["message_id_2"]))
            await self.bot.db.set_board_message(board_id, None, part=2)

        await self.bot.db.set_board_signature(board_id, sig)
        return True

    async def _place(self, channel, board, board_id: int, part: int,
                     embed, view) -> bool:
        """Edit this part's message if it still exists, else post a new one."""
        column = "message_id" if part == 1 else "message_id_2"
        existing = int(board[column]) if board is not None and board[column] else None

        if existing is not None:
            if await self._edit(channel, existing, embed, view) is not None:
                return True
            # Deleted by someone, or too old to edit: start a new one.
            await self.bot.db.set_board_message(board_id, None, part=part)

        message = await self._post(channel, embed, view)
        if message is None:
            return False
        await self.bot.db.set_board_message(board_id, message.id, part=part)
        if part == 1:
            await self._pin(message)
        return True

    async def _retire(self, channel, message_id: int) -> None:
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.info("board: could not remove the spare part: %s", exc)

    async def _store(self, board_id: int, live: list[dict]) -> None:
        rows = []
        for match in live:
            teams = opponents(match)
            rows.append({
                "match_id": int(match["id"]),
                "begin_at": match.get("begin_at"),
                "team_a": (teams[0]["id"], teams[0]["name"]),
                "team_b": (teams[1]["id"], teams[1]["name"]),
                "tournament_id": match_tournament_id(match),
                "tournament_name": (match.get("tournament") or {}).get("name"),
            })
        await self.bot.db.set_board_matches(board_id, rows)

    def _view(self, board_id: int, bank: list[dict], tz, *,
              controls: bool = True, overflow: bool = False):
        """One message's bank of buttons: a match per row, two teams each.

        A pair on its own row is what makes the choice readable — four buttons
        side by side stop reading as two matches — so five matches is a
        message's worth, and the controls take the fifth row only when four or
        fewer matches need it.
        """
        if not bank and not controls:
            return None
        view = discord.ui.View(timeout=None)
        icons = self.bot.icons
        for row, match in enumerate(bank[:TAPPABLE_PER_PART]):
            teams = opponents(match)
            when = _label(match, tz)
            for side, team in enumerate(teams[:2]):
                view.add_item(
                    BoardVoteButton(
                        board_id, int(match["id"]), side,
                        label=f"{when}{team['name']}",
                        emoji=icons.partial(team),
                        row=row,
                    )
                )
        if controls and len(bank) <= CONTROLS_FIT_AT:
            if overflow:
                # Only past ten open matches, where no number of buttons helps.
                view.add_item(PickerButton(board_id, label="🎲 Later matches"))
            view.add_item(MyPicksButton(board_id))
        return view if view.children else None

    async def _post(self, channel, embed, view):
        try:
            return await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            log.warning("board: cannot post in %s", channel.id)
        except discord.HTTPException as exc:
            log.warning("board: failed to post in %s: %s", channel.id, exc)
        return None

    async def _edit(self, channel, message_id: int, embed, view):
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        try:
            return await message.edit(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("board: failed to edit %s: %s", message_id, exc)
            return None

    async def _pin(self, message) -> None:
        """Pin the board, retiring the previous one from the same channel.

        Pins are the server's, and capped at fifty. A board is replaced rather
        than accumulated, so at most one per channel is ever held.
        """
        try:
            for pinned in await message.channel.pins():
                if (pinned.author.id == self.bot.user.id
                        and pinned.id != message.id
                        and pinned.embeds
                        and str(pinned.embeds[0].title or "").startswith("🗓️")):
                    await pinned.unpin()
            await message.pin()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.info("board: could not pin in %s: %s", message.channel.id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Board(bot))
