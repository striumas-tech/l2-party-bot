import asyncpg
import os
from zoneinfo import ZoneInfo, available_timezones
import re
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict

import discord
from discord import app_commands
from discord.app_commands import Choice

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1149113323200200825

intents = discord.Intents.default()
intents.members = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ================= STORAGE =================

active_parties: Dict[str, dict] = {}
user_party_map: Dict[int, str] = {}
zone_counters: Dict[str, int] = {}

db_pool = None
ALL_TIMEZONES = sorted(available_timezones())

# ================= ROLE DATA =================

ROLE_DATA = {
    "tank": {"icon": "🛡", "name": "Tank"},
    "wc": {"icon": "📜", "name": "Warcryer"},
    "pp": {"icon": "📜", "name": "Prophet"},
    "bd": {"icon": "💃", "name": "Bladedancer"},
    "sws": {"icon": "🎼", "name": "Sword Singer"},
    "se": {"icon": "✨", "name": "Shillien Elder"},
    "ee": {"icon": "✨", "name": "Elven Elder"},
    "bs": {"icon": "✨", "name": "Bishop"},
    "dd": {"icon": "⚔️", "name": "DD"},
    "mage": {"icon": "🔥", "name": "Mage"},
    "sum": {"icon": "🐺", "name": "Summoner"},
    "spoil": {"icon": "💰", "name": "Spoiler"},
}

# ================= UTILITIES =================

async def parse_user_time(time_str: str, interaction: discord.Interaction):
    if not re.match(r"^\d{2}:\d{2}$", time_str):
        return None

    hour, minute = map(int, time_str.split(":"))

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT timezone FROM user_timezones WHERE user_id = $1",
            interaction.user.id
        )

    if not row:
        return None

    user_tz = ZoneInfo(row["timezone"])
    now_local = datetime.now(user_tz)

    start_local = now_local.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0
    )

    if start_local <= now_local:
        start_local += timedelta(days=1)

    return start_local.astimezone(timezone.utc)


def progress_bar(current, total, length=14):
    if total == 0:
        return "░" * length

    filled = int(length * current / total)
    return "█" * filled + "░" * (length - filled)


def generate_party_id(zone: str):
    zone = zone.upper()
    zone_counters[zone] = zone_counters.get(zone, 0) + 1
    return f"{zone}-{zone_counters[zone]:02d}"

# ================= EMBED =================

def build_embed(party):
    now = datetime.now(timezone.utc)
    start_ts = int(party["start_time"].timestamp())

    requested_total = sum(party["roles_required"].values())

    if party["leader_class"] in party["roles_required"]:
        total = requested_total
    else:
        total = requested_total + 1

    current = len(party["members"])

    if current >= total:
        status = "🟣 FULL"
        color = discord.Color.purple()
    elif now >= party["start_time"]:
        status = "🔴 STARTED"
        color = discord.Color.red()
    elif (party["start_time"] - now).total_seconds() <= 600:
        status = "🟠 FORMING"
        color = discord.Color.orange()
    else:
        status = "🟢 RECRUITING"
        color = discord.Color.green()

    embed = discord.Embed(
        title=f"⚔ {party['zone'].upper()} PARTY LOBBY",
        color=color
    )

    embed.add_field(
        name="⏱ RAID TIMER",
        value=f"<t:{start_ts}:t>\n<t:{start_ts}:R>",
        inline=False
    )

    leader_member = party["guild"].get_member(party["leader_id"])
    leader_name = leader_member.display_name if leader_member else "Unknown"

    embed.add_field(
        name="👑 LEADER",
        value=f"{leader_name} • {ROLE_DATA[party['leader_class']]['name']}",
        inline=False
    )

    groups = {
        "🛡 TANK": ["tank"],
        "🧩 SUPPORT": ["wc", "pp", "bd", "sws", "se", "ee", "bs"],
        "⚔️ DPS": ["dd", "mage", "sum", "spoil"],
    }

    for title, roles in groups.items():
        section_text = ""

        for role in roles:
            if role in party["roles_required"]:
                required = party["roles_required"][role]

                role_members = [
                    uid for uid, r in party["members"].items()
                    if r == role
                ]

                filled = len(role_members)
                mark = "🟢" if filled >= required else "❌"

                icon = ROLE_DATA[role]["icon"]
                name = ROLE_DATA[role]["name"]

                section_text += f"{mark} {icon} **{name}** `{filled}/{required}`\n"

                for uid in role_members:
                    member = party["guild"].get_member(uid)
                    if member:
                        crown = " 👑" if uid == party["leader_id"] else ""
                        section_text += f" • {member.display_name}{crown}\n"

        if section_text:
            embed.add_field(name=title, value=section_text, inline=False)

    embed.add_field(
        name="📊 PARTY CAPACITY",
        value=f"`{progress_bar(current, total)}`\n**{current}/{total} Members**",
        inline=False
    )

    embed.add_field(name="📌 STATUS", value=f"**{status}**", inline=False)

    return embed
    
    bot.run(TOKEN)
