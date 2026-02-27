import os
import re
import time
import asyncio
from datetime import datetime, timezone
from typing import Dict

import discord
from discord import app_commands
from discord.app_commands import Choice

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1149113323200200825

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

MAX_PARTY_SIZE = 9

active_parties: Dict[int, dict] = {}
user_party_map: Dict[int, int] = {}
button_cooldowns: Dict[int, float] = {}

party_counter = 100


# ==================================================
# Utilities
# ==================================================

def parse_utc_time(time_str: str):
    if not re.match(r"^\d{2}:\d{2}$", time_str):
        return None

    hour, minute = map(int, time_str.split(":"))
    if hour > 23 or minute > 59:
        return None

    now = datetime.now(timezone.utc)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def check_cooldown(user_id: int):
    now = time.time()
    last = button_cooldowns.get(user_id, 0)

    if now - last < 2:
        return False

    button_cooldowns[user_id] = now
    return True


def progress_bar(current, total, length=14):
    if total == 0:
        return "░" * length
    filled = int(length * current / total)
    return "█" * filled + "░" * (length - filled)


def party_capacity(party):
    """REAL max capacity always 9"""
    return MAX_PARTY_SIZE


# ==================================================
# EMBED
# ==================================================

def build_embed(party):
    now = datetime.now(timezone.utc)
    start_ts = int(party["start_time"].timestamp())

    current_members = len(party["members"])
    max_capacity = party_capacity(party)

    # Status
    if current_members >= max_capacity:
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
        title=f"⚔️ {party['zone'].upper()} RAID LOBBY",
        color=color
    )

    embed.add_field(
        name="⏱ RAID TIMER",
        value=f"🕒 **<t:{start_ts}:t>**\n⏳ <t:{start_ts}:R>",
        inline=False
    )

    embed.add_field(
        name="👑 LEADER",
        value=f"<@{party['leader_id']}> • **{party['leader_class'].upper()}**",
        inline=False
    )

    # Role display
    role_text = ""
    for role, required in party["roles_required"].items():
        filled = sum(1 for r in party["members"].values() if r == role)
        mark = "✔️" if filled >= required else "❌"
        role_text += f"{mark} **{role.upper()}** `{filled}/{required}`\n"

    if not role_text:
        role_text = "Open composition"

    embed.add_field(
        name="🧩 ROLES",
        value=role_text,
        inline=False
    )

    embed.add_field(
        name="📊 RAID CAPACITY",
        value=f"`{progress_bar(current_members, max_capacity)}`\n**{current_members}/{max_capacity} Members**",
        inline=False
    )

    embed.add_field(
        name="📌 STATUS",
        value=f"**{status}**",
        inline=False
    )

    embed.set_footer(text="Lineage II Raid System")

    return embed


# ==================================================
# BUTTONS
# ==================================================

class PartyView(discord.ui.View):
    def __init__(self, party_id):
        super().__init__(timeout=None)
        self.party_id = party_id

        party = active_parties.get(party_id)
        if not party:
            return

        # Join buttons only if role still needed
        for role, required in party["roles_required"].items():
            filled = sum(1 for r in party["members"].values() if r == role)
            if filled < required:
                self.add_item(JoinButton(party_id, role))

        self.add_item(LeaveButton(party_id))
        self.add_item(CloseButton(party_id))


class JoinButton(discord.ui.Button):
    def __init__(self, party_id, role):
        super().__init__(
            label=f"Join {role.upper()}",
            style=discord.ButtonStyle.primary
        )
        self.party_id = party_id
        self.role = role

    async def callback(self, interaction: discord.Interaction):

        if not check_cooldown(interaction.user.id):
            await interaction.response.send_message("Cooldown 2s.", ephemeral=True)
            return

        party = active_parties.get(self.party_id)
        if not party:
            await interaction.response.send_message("Party not found.", ephemeral=True)
            return

        if interaction.user.id in user_party_map:
            await interaction.response.send_message("Already in party.", ephemeral=True)
            return

        if len(party["members"]) >= MAX_PARTY_SIZE:
            await interaction.response.send_message("Party full.", ephemeral=True)
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
            await interaction.response.send_message("Party gone.", ephemeral=True)
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


class CloseButton(discord.ui.Button):
    def __init__(self, party_id):
        super().__init__(label="Close", style=discord.ButtonStyle.danger)
        self.party_id = party_id

    async def callback(self, interaction: discord.Interaction):

        party = active_parties.get(self.party_id)
        if not party or interaction.user.id != party["leader_id"]:
            await interaction.response.send_message("Leader only.", ephemeral=True)
            return

        await interaction.response.defer()

        await interaction.message.delete()

        del active_parties[self.party_id]
        for uid in list(user_party_map):
            if user_party_map[uid] == self.party_id:
                del user_party_map[uid]


# ==================================================
# SLASH COMMAND
# ==================================================

@tree.command(name="lfp", description="Create party", guild=discord.Object(id=GUILD_ID))
@app_commands.choices(
    leader_class=[
        Choice(name="Destro", value="destro"),
        Choice(name="WC", value="wc"),
        Choice(name="PP", value="pp"),
        Choice(name="BD", value="bd"),
        Choice(name="SWS", value="sws"),
        Choice(name="SE", value="se"),
        Choice(name="EE", value="ee"),
        Choice(name="BS", value="bs"),
        Choice(name="Spoiler", value="spoil"),
        Choice(name="DD", value="dd"),
        Choice(name="Leacher", value="leacher"),
        Choice(name="Random", value="random"),
    ]
)
async def lfp(
    interaction: discord.Interaction,
    zone: str,
    time: str,
    leader_class: Choice[str],
    wc: int = 0,
    destro: int = 0,
    pp: int = 0,
    bd: int = 0,
    sws: int = 0,
    se: int = 0,
    ee: int = 0,
    bs: int = 0,
    dd: int = 0,
    spoil: int = 0,
    leacher: int = 0,
    random: int = 0
):

    global party_counter

    if interaction.user.id in user_party_map:
        await interaction.response.send_message("Already in party.", ephemeral=True)
        return

    start_time = parse_utc_time(time)
    if not start_time:
        await interaction.response.send_message("Invalid time (HH:MM).", ephemeral=True)
        return

    roles_required = {
        k: v for k, v in {
            "wc": wc,
            "destro": destro,
            "pp": pp,
            "bd": bd,
            "sws": sws,
            "se": se,
            "ee": ee,
            "bs": bs,
            "dd": dd,
            "spoil": spoil,
            "leacher": leacher,
            "random": random,
        }.items() if v > 0
    }

    party_counter += 1
    party_id = party_counter

    party = {
        "id": party_id,
        "zone": zone,
        "leader_id": interaction.user.id,
        "leader_class": leader_class.value,
        "start_time": start_time,
        "roles_required": roles_required,
        "members": {interaction.user.id: leader_class.value},
        "message_id": None,
        "channel_id": interaction.channel.id,
        "reminded": False
    }

    active_parties[party_id] = party
    user_party_map[interaction.user.id] = party_id

    await interaction.response.send_message(
        embed=build_embed(party),
        view=PartyView(party_id)
    )

    sent = await interaction.original_response()
    party["message_id"] = sent.id


@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Logged in as {bot.user}")


bot.run(TOKEN)
