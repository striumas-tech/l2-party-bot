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
    start_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)

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
    total = requested_total if party["leader_class"] in party["roles_required"] else requested_total + 1
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
                    uid for uid, r in party["members"].items() if r == role
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


# ================= BUTTONS =================

class PartyView(discord.ui.View):
    def __init__(self, party_id, viewer_id=None):
        super().__init__(timeout=None)
        self.party_id = party_id

        party = active_parties.get(party_id)
        if not party:
            return

        for role, required in party["roles_required"].items():
            filled = sum(1 for r in party["members"].values() if r == role)
            if filled < required:
                self.add_item(JoinButton(party_id, role))

        self.add_item(LeaveButton(party_id))

        if viewer_id == party["leader_id"]:
            self.add_item(CancelButton(party_id))


class JoinButton(discord.ui.Button):
    def __init__(self, party_id, role):
        super().__init__(label=f"Join {ROLE_DATA[role]['name']}", style=discord.ButtonStyle.primary)
        self.party_id = party_id
        self.role = role

    async def callback(self, interaction: discord.Interaction):

        party = active_parties.get(self.party_id)
        if not party:
            return

        if interaction.user.id in user_party_map:
            await interaction.response.send_message("Already in party.", ephemeral=True)
            return

        await interaction.response.defer()

        party["members"][interaction.user.id] = self.role
        user_party_map[interaction.user.id] = self.party_id

        await interaction.message.edit(
            embed=build_embed(party),
            view=PartyView(self.party_id, interaction.user.id)
        )


class LeaveButton(discord.ui.Button):
    def __init__(self, party_id):
        super().__init__(label="Leave", style=discord.ButtonStyle.secondary)
        self.party_id = party_id

    async def callback(self, interaction: discord.Interaction):

        party = active_parties.get(self.party_id)
        if not party:
            return

        await interaction.response.defer()

        if interaction.user.id == party["leader_id"]:
            await interaction.message.delete()
            del active_parties[self.party_id]
            return

        party["members"].pop(interaction.user.id, None)
        user_party_map.pop(interaction.user.id, None)

        await interaction.message.edit(
            embed=build_embed(party),
            view=PartyView(self.party_id, interaction.user.id)
        )


class CancelButton(discord.ui.Button):
    def __init__(self, party_id):
        super().__init__(label="Cancel Party", style=discord.ButtonStyle.danger)
        self.party_id = party_id

    async def callback(self, interaction: discord.Interaction):

        party = active_parties.get(self.party_id)
        if not party:
            return

        if interaction.user.id != party["leader_id"]:
            await interaction.response.send_message(
                "Only party leader can cancel.",
                ephemeral=True
            )
            return

        await interaction.response.defer()
        await interaction.message.delete()
        del active_parties[self.party_id]


# ================= SCHEDULER =================

async def party_scheduler():
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = datetime.now(timezone.utc)

        for party_id, party in list(active_parties.items()):
            channel = bot.get_channel(party["channel_id"])
            if not channel:
                continue

            start = party["start_time"]

            if now > start and (now - start).total_seconds() >= 1800:
                try:
                    msg = await channel.fetch_message(party["message_id"])
                    await msg.delete()
                except:
                    pass

                del active_parties[party_id]
                await channel.send(
                    f"❌ **{party['zone'].upper()} PARTY expired (30 minutes passed).**"
                )
                continue

            try:
                msg = await channel.fetch_message(party["message_id"])
                await msg.edit(
                    embed=build_embed(party),
                    view=PartyView(party_id, party["leader_id"])
                )
            except:
                pass

        await asyncio.sleep(30)


# ================= READY =================

@bot.event
async def on_ready():
    global db_pool

    db_pool = await asyncpg.create_pool(
        os.getenv("DATABASE_URL"),
        ssl="require"
    )

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_timezones (
                user_id BIGINT PRIMARY KEY,
                timezone TEXT NOT NULL
            );
        """)

    bot.loop.create_task(party_scheduler())

    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Logged in as {bot.user}")


bot.run(TOKEN)
