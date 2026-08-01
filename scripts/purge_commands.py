"""List or delete this bot's registered slash commands.

Discord stores application commands on **its** side, per scope, until something
overwrites that scope. They stay in the picker with the bot offline, so a
stopped container never clears them — and commands registered to a guild are
invisible to a global sync, which is how stale sets survive a redeploy.

This talks straight to the REST API, so it works with the container down.

    # see what Discord currently has (safe, read-only)
    python scripts/purge_commands.py

    # clear a specific server's leftovers
    python scripts/purge_commands.py --purge-guild 123456789012345678

    # clear the global set (the bot re-registers it on next start)
    python scripts/purge_commands.py --purge-global

The token is read from ``DISCORD_TOKEN`` in the environment or a local ``.env``
and is never printed or written anywhere.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import aiohttp

API = "https://discord.com/api/v10"
ROOT = Path(__file__).resolve().parent.parent


def load_token() -> str:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("DISCORD_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not token:
        sys.exit(
            "No DISCORD_TOKEN found. Set it in the environment or in .env — "
            "don't pass it on the command line, where it lands in your shell "
            "history."
        )
    return token


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--purge-global", action="store_true",
                    help="delete every globally registered command")
    ap.add_argument("--purge-guild", metavar="GUILD_ID", action="append",
                    default=[], help="delete a server's guild-scoped commands")
    ap.add_argument("--purge-all-guilds", action="store_true",
                    help="delete guild-scoped commands in every server the bot is in")
    args = ap.parse_args()

    token = load_token()
    headers = {"Authorization": f"Bot {token}"}

    async with aiohttp.ClientSession(headers=headers) as http:
        async with http.get(f"{API}/users/@me") as r:
            if r.status == 401:
                sys.exit("Discord rejected the token (401). Is it the bot token?")
            r.raise_for_status()
            me = await r.json()
        app_id = me["id"]
        print(f"Bot: {me.get('username')}  (application {app_id})\n")

        async def show(scope: str, url: str) -> list:
            async with http.get(url) as r:
                if r.status == 403:
                    print(f"{scope}: no access (bot not in that server?)")
                    return []
                r.raise_for_status()
                cmds = await r.json()
            names = ", ".join(sorted(c["name"] for c in cmds)) or "none"
            print(f"{scope}: {len(cmds)} — {names}")
            return cmds

        async def purge(scope: str, url: str) -> None:
            async with http.put(url, json=[]) as r:
                r.raise_for_status()
            print(f"  ✔ cleared {scope}")

        global_url = f"{API}/applications/{app_id}/commands"
        await show("global", global_url)

        async with http.get(f"{API}/users/@me/guilds") as r:
            r.raise_for_status()
            guilds = await r.json()
        print(f"\nIn {len(guilds)} server(s):")
        for g in guilds:
            await show(
                f"  {g['name']} ({g['id']})",
                f"{API}/applications/{app_id}/guilds/{g['id']}/commands",
            )

        targets = list(args.purge_guild)
        if args.purge_all_guilds:
            targets = [g["id"] for g in guilds]

        if not args.purge_global and not targets:
            print("\nRead-only. Re-run with --purge-global or --purge-guild <id> "
                  "to delete anything.")
            return

        print()
        if args.purge_global:
            await purge("global commands", global_url)
        for gid in targets:
            await purge(
                f"guild {gid}",
                f"{API}/applications/{app_id}/guilds/{gid}/commands",
            )
        print("\nStart the bot to re-register its current command set.")


asyncio.run(main())
