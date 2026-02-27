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

    # Get user's timezone from DB
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT timezone FROM user_timezones WHERE user_id = $1",
            interaction.user.id
        )

    if not row:
        return None  # user must set timezone first

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
    def __init__(self, party_id):
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

        # Only leader sees Cancel
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
            view=PartyView(self.party_id)
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
            for uid in list(user_party_map):
                if user_party_map[uid] == self.party_id:
                    del user_party_map[uid]
            return

        party["members"].pop(interaction.user.id, None)
        user_party_map.pop(interaction.user.id, None)

        await interaction.message.edit(
            embed=build_embed(party),
            view=PartyView(self.party_id)
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
        for uid in list(user_party_map):
            if user_party_map[uid] == self.party_id:
                del user_party_map[uid]

async def timezone_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    matches = [
        tz for tz in ALL_TIMEZONES
        if current.lower() in tz.lower()
    ]

    return [
        app_commands.Choice(name=tz, value=tz)
        for tz in matches[:25]
    ]

# ================= SLASH COMMAND =================

@tree.command(name="lfp", description="Create party", guild=discord.Object(id=GUILD_ID))
@app_commands.choices(
    leader_class=[Choice(name=v["name"], value=k) for k, v in ROLE_DATA.items()]
)
async def lfp(
    interaction: discord.Interaction,
    zone: str,
    time: str,
    leader_class: Choice[str],
    tank: int = 0,
    wc: int = 0, pp: int = 0, bd: int = 0, sws: int = 0,
    se: int = 0, ee: int = 0, bs: int = 0,
    dd: int = 0, mage: int = 0, sum: int = 0, spoil: int = 0,
):

    start_time = await parse_user_time(time, interaction)
    if not start_time:
        await interaction.response.send_message(
            "Invalid time or you must set timezone first using /settimezone",
            ephemeral=True
        )
        return

    roles_input = {
        "tank": tank,
        "wc": wc, "pp": pp, "bd": bd, "sws": sws,
        "se": se, "ee": ee, "bs": bs,
        "dd": dd, "mage": mage, "sum": sum, "spoil": spoil,
    }

    roles_required = {k: v for k, v in roles_input.items() if v > 0}

    # Ensure leader role is always present
    if leader_class.value not in roles_required:
        roles_required[leader_class.value] = 1

    party_id = generate_party_id(zone)

    party = {
        "guild": interaction.guild,
        "zone": zone,
        "party_id": party_id,
        "leader_id": interaction.user.id,
        "leader_class": leader_class.value,
        "start_time": start_time,
        "roles_required": roles_required,
        "members": {interaction.user.id: leader_class.value},
        "channel_id": interaction.channel.id,
        "reminded": False,
    }

    active_parties[party_id] = party
    user_party_map[interaction.user.id] = party_id

    await interaction.response.send_message(
        embed=build_embed(party),
        view=PartyView(party_id)
    )

@tree.command(
    name="settimezone",
    description="Set your timezone (example: Europe/Berlin)",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.autocomplete(timezone=timezone_autocomplete)
async def settimezone(
    interaction: discord.Interaction,
    timezone: str
):
    # Validate timezone
    try:
        ZoneInfo(timezone)
    except:
        await interaction.response.send_message(
            "Invalid timezone selected.",
            ephemeral=True
        )
        return

    # Save to database
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_timezones (user_id, timezone)
            VALUES ($1, $2)
            ON CONFLICT (user_id)
            DO UPDATE SET timezone = $2
        """, interaction.user.id, timezone)

    await interaction.response.send_message(
        f"✅ Timezone set to **{timezone}**",
        ephemeral=True
    )

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

    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Logged in as {bot.user}")

bot.run(TOKEN)
