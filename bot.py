import os
import re
import time
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict

import discord
from discord import app_commands
from discord.app_commands import Choice

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1149113323200200825

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# =========================
# STORAGE
# =========================

active_parties: Dict[str, dict] = {}
user_party_map: Dict[int, str] = {}
zone_counters: Dict[str, int] = {}
button_cooldowns: Dict[int, float] = {}

# =========================
# ROLE ICONS
# =========================

ROLE_ICONS = {
    "tank": "🛡",
    "wc": "📜",
    "pp": "📜",
    "bd": "💃",
    "sws": "🎼",
    "se": "✨",
    "ee": "✨",
    "bs": "✨",
    "dd": "⚔️",
    "mage": "🔥",
    "sum": "🐺",
    "spoil": "💰",
}

# =========================
# UTILITIES
# =========================

def parse_utc_time(time_str: str):
    if not re.match(r"^\d{2}:\d{2}$", time_str):
        return None

    hour, minute = map(int, time_str.split(":"))
    now = datetime.now(timezone.utc)

    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if start <= now:
        start += timedelta(days=1)

    return start


def progress_bar(current, total, length=14):
    if total == 0:
        return "░" * length
    filled = int(length * current / total)
    return "█" * filled + "░" * (length - filled)


def generate_party_id(zone: str):
    zone = zone.upper()
    zone_counters[zone] = zone_counters.get(zone, 0) + 1
    return f"{zone}-{zone_counters[zone]:02d}"


# =========================
# EMBED
# =========================

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

    embed.add_field(
        name="👑 LEADER",
        value=f"<@{party['leader_id']}> • **{party['leader_class'].upper()}**",
        inline=False
    )

    groups = {
        "🛡 TANKS": ["tank"],
        "🧩 SUPPORT": ["wc", "pp", "bd", "sws", "se", "ee", "bs"],
        "⚔️ DPS": ["dd", "mage", "sum", "spoil"],
    }

    for title, roles in groups.items():
        text = ""
        for role in roles:
            if role in party["roles_required"]:
                required = party["roles_required"][role]
                filled = sum(1 for r in party["members"].values() if r == role)
                icon = ROLE_ICONS.get(role, "")
                mark = "🟢" if filled >= required else "❌"
                text += f"{mark} {icon} **{role.upper()}** `{filled}/{required}`\n"

        if text:
            embed.add_field(name=title, value=text, inline=True)

    embed.add_field(
        name="📊 PARTY CAPACITY",
        value=f"`{progress_bar(current, total)}`\n**{current}/{total} Members**",
        inline=False
    )

    embed.add_field(name="📌 STATUS", value=f"**{status}**", inline=False)

    return embed


# =========================
# BUTTONS
# =========================

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
        self.add_item(CancelButton(party_id))


class JoinButton(discord.ui.Button):
    def __init__(self, party_id, role):
        super().__init__(label=f"Join {role.upper()}", style=discord.ButtonStyle.primary)
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
                "Only leader can cancel.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        await interaction.message.delete()

        del active_parties[self.party_id]
        for uid in list(user_party_map):
            if user_party_map[uid] == self.party_id:
                del user_party_map[uid]


# =========================
# SLASH COMMAND
# =========================

@tree.command(name="lfp", description="Create party", guild=discord.Object(id=GUILD_ID))
@app_commands.choices(
    leader_class=[Choice(name=k.upper(), value=k) for k in ROLE_ICONS.keys()]
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

    start_time = parse_utc_time(time)
    if not start_time:
        await interaction.response.send_message("Invalid time format (HH:MM).", ephemeral=True)
        return

    roles_required = {
        k: v for k, v in {
            "tank": tank,
            "wc": wc, "pp": pp, "bd": bd, "sws": sws,
            "se": se, "ee": ee, "bs": bs,
            "dd": dd, "mage": mage, "sum": sum, "spoil": spoil,
        }.items() if v > 0
    }

    party_id = generate_party_id(zone)

    party = {
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

    sent = await interaction.original_response()
    party["message_id"] = sent.id


# =========================
# SCHEDULER
# =========================

async def party_scheduler():
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = datetime.now(timezone.utc)

        for party_id, party in list(active_parties.items()):

            channel = bot.get_channel(party["channel_id"])
            if not channel:
                continue

            start = party["start_time"]

            # 10 min reminder
            if not party["reminded"]:
                seconds_left = (start - now).total_seconds()
                if 0 < seconds_left <= 600:
                    mentions = " ".join(f"<@{uid}>" for uid in party["members"])
                    await channel.send(
                        f"⏰ **{party['zone'].upper()} PARTY LOBBY starts in 10 minutes!**\n{mentions}"
                    )
                    party["reminded"] = True

            # 30 min expire
            if now > start and (now - start).total_seconds() >= 1800:
                try:
                    msg = await channel.fetch_message(party["message_id"])
                    await msg.delete()
                except:
                    pass

                del active_parties[party_id]
                for uid in list(user_party_map):
                    if user_party_map[uid] == party_id:
                        del user_party_map[uid]

                await channel.send(
                    f"❌ **{party['zone'].upper()} PARTY expired (30 minutes passed).**"
                )
                continue

            # auto update embed color
            try:
                msg = await channel.fetch_message(party["message_id"])
                await msg.edit(embed=build_embed(party), view=PartyView(party_id))
            except:
                pass

        await asyncio.sleep(30)


@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    bot.loop.create_task(party_scheduler())
    print(f"Logged in as {bot.user}")


bot.run(TOKEN)
