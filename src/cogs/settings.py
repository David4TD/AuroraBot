"""`/settings` — see this server's configuration, change it, or reset it.

Preferences that used to be env-only live here per server: the daily schedule's
hour and timezone, and how long before kick-off reminders fire. Anything not
overridden shows "(default)" and follows the deployment's value, so an untouched
server behaves exactly as it did.

Resets are **per server and confirmed twice**. Each one names the exact row
counts it will delete before the second click, because there is no undo and
"reset everything" is easy to click by accident. A member's prediction history
in *other* servers, and their lifetime points, are never touched.
"""
from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.embeds import AMBER, BRAND, GREEN
from ..utils.games import label_for
from ..utils.guildprefs import LEAD_CHOICES, describe

log = logging.getLogger("aurorabot.cogs.settings")

RESET_TARGETS = {
    "alerts": ("🔔 Alerts", "every subscription, plus posted digests"),
    "games": ("🎮 Games", "the server's game selection — the bot goes idle"),
    "predictions": ("🎲 Predictions", "this server's picks, points and leaderboards"),
    "settings": ("⚙️ Preferences", "back to the deployment defaults"),
    "all": ("💥 Everything", "all of the above, for this server"),
}


def can_manage(user) -> bool:
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


async def settings_embed(bot, guild_id: int, user_id: int) -> discord.Embed:
    overrides = await bot.db.guild_settings(guild_id)
    current = describe(bot.settings, overrides)
    try:
        counts = await bot.db.guild_summary(guild_id)
    except Exception:  # noqa: BLE001 - the panel must still render
        log.exception("could not summarise guild %s", guild_id)
        counts = {"games": 0, "alerts": 0, "predictions": 0, "digests": 0}
    enabled = await bot.db.enabled_games(guild_id)
    favourite = await bot.db.favorite_game(user_id)

    embed = discord.Embed(
        title="⚙️ AuroraBot settings",
        description="Values marked *(default)* follow the bot's configuration; "
        "anything you set here overrides them for this server only.",
        color=BRAND if enabled else AMBER,
    )
    embed.add_field(
        name="📅 Daily schedule",
        value=f"Posts at **{current['digest_hour']}**\n"
        f"Timezone **{current['digest_tz']}**",
        inline=True,
    )
    embed.add_field(
        name="🔔 Alert reminder",
        value=f"**{current['alert_lead_minutes']}** before kick-off",
        inline=True,
    )
    embed.add_field(
        name="🎮 Games",
        value=", ".join(sorted(label_for(g) for g in enabled)) or
        "_None — run `/games`_",
        inline=False,
    )
    embed.add_field(
        name="📊 This server's data",
        value=f"{counts['alerts']} alert(s) · {counts['predictions']} prediction(s) "
        f"· {counts['digests']} digest(s)",
        inline=False,
    )
    embed.add_field(
        name="👤 You",
        value=f"Default game: **{label_for(favourite) if favourite else 'not set'}**"
        + ("" if favourite else " — try `/setgame`"),
        inline=False,
    )
    embed.set_footer(text="Changes apply to this server only")
    return embed


class HourSelect(discord.ui.Select):
    def __init__(self, current: int) -> None:
        super().__init__(
            placeholder="Daily schedule hour…",
            options=[
                discord.SelectOption(
                    label=f"{h:02d}:00", value=str(h), default=h == current
                )
                for h in range(24)
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        hour = int(self.values[0])
        await interaction.client.db.set_guild_setting(
            interaction.guild_id, "digest_hour", hour, interaction.user.id
        )
        await interaction.response.edit_message(
            embed=await settings_embed(
                interaction.client, interaction.guild_id, interaction.user.id
            ),
            view=await SettingsView.build(interaction.client, interaction.guild_id),
        )


class LeadSelect(discord.ui.Select):
    def __init__(self, current: int) -> None:
        super().__init__(
            placeholder="Reminder lead time…",
            options=[
                discord.SelectOption(
                    label=f"{m} minutes before", value=str(m), default=m == current
                )
                for m in LEAD_CHOICES
            ],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.db.set_guild_setting(
            interaction.guild_id, "alert_lead_minutes",
            int(self.values[0]), interaction.user.id,
        )
        await interaction.response.edit_message(
            embed=await settings_embed(
                interaction.client, interaction.guild_id, interaction.user.id
            ),
            view=await SettingsView.build(interaction.client, interaction.guild_id),
        )


class TimezoneModal(discord.ui.Modal, title="Daily schedule timezone"):
    """Free text, because there are ~600 zones and a select holds 25."""

    zone = discord.ui.TextInput(
        label="IANA timezone",
        placeholder="Australia/Sydney, Europe/Berlin, America/New_York…",
        max_length=64,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.zone).strip()
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            await interaction.response.send_message(
                f"**{name}** isn't a timezone I recognise. Use an IANA name like "
                f"`Europe/Berlin` — the full list is on "
                f"<https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>.",
                ephemeral=True,
            )
            return
        await interaction.client.db.set_guild_setting(
            interaction.guild_id, "digest_tz", name, interaction.user.id
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"📅 Daily schedule timezone set to **{name}**.",
                color=GREEN,
            ),
            ephemeral=True,
        )


class TimezoneButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Set timezone", emoji="🌍",
                         style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TimezoneModal())


class ResetButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Reset…", emoji="🗑️",
                         style=discord.ButtonStyle.danger, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need **Manage Server** to reset anything.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🗑️ Reset this server",
                description="Pick what to clear. You'll get one more "
                "confirmation with exact numbers — there's no undo.",
                color=AMBER,
            ),
            view=ResetView(),
        )


class ResetSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="What should I clear?",
            options=[
                discord.SelectOption(label=label, value=key, description=desc[:100])
                for key, (label, desc) in RESET_TARGETS.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        target = self.values[0]
        counts = await interaction.client.db.guild_summary(interaction.guild_id)
        affected = {
            "alerts": ["alerts", "digests"],
            "games": ["games"],
            "predictions": ["predictions"],
            "settings": [],
            "all": ["games", "alerts", "predictions", "digests"],
        }[target]
        detail = ", ".join(f"**{counts[k]}** {k}" for k in affected) or "your preferences"
        label = RESET_TARGETS[target][0]
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"Confirm: reset {label}",
                description=f"This deletes {detail} for **this server**.\n"
                f"Nobody's history in other servers is affected.\n\n"
                f"**This cannot be undone.**",
                color=AMBER,
            ),
            view=ConfirmResetView(target),
        )


class ConfirmResetButton(discord.ui.Button):
    def __init__(self, target: str) -> None:
        self.target = target
        super().__init__(label="Yes, reset it", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need **Manage Server** to reset anything.", ephemeral=True
            )
            return
        removed = await interaction.client.db.reset_guild(
            interaction.guild_id, what=self.target
        )
        log.info(
            "Guild %s reset '%s' by %s: %s",
            interaction.guild_id, self.target, interaction.user.id, removed,
        )
        summary = "\n".join(f"• {n} {label}" for label, n in removed.items()) \
            or "Nothing was stored, so nothing changed."
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🗑️ Reset complete",
                description=summary,
                color=GREEN,
            ),
            view=None,
        )


class CancelButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=discord.Embed(description="Nothing was changed.", color=BRAND),
            view=None,
        )


class ConfirmResetView(discord.ui.View):
    def __init__(self, target: str) -> None:
        super().__init__(timeout=120)
        self.add_item(ConfirmResetButton(target))
        self.add_item(CancelButton())


class ResetView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(ResetSelect())
        self.add_item(CancelButton())


class SettingsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)

    @classmethod
    async def build(cls, bot, guild_id: int) -> "SettingsView":
        from ..utils.guildprefs import alert_lead, digest_hour

        overrides = await bot.db.guild_settings(guild_id)
        view = cls()
        view.add_item(HourSelect(digest_hour(bot.settings, overrides)))
        view.add_item(LeadSelect(alert_lead(bot.settings, overrides)))
        view.add_item(TimezoneButton())
        view.add_item(ResetButton())
        return view


class SettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="settings", description="See, change or reset this server's settings."
    )
    @app_commands.guild_only()
    async def settings(self, interaction: discord.Interaction) -> None:
        embed = await settings_embed(
            self.bot, interaction.guild_id, interaction.user.id
        )
        if not can_manage(interaction.user):
            embed.set_footer(
                text="A member with Manage Server can change these with /settings"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await interaction.response.send_message(
            embed=embed,
            view=await SettingsView.build(self.bot, interaction.guild_id),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SettingsCog(bot))
