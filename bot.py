import os
import re
from datetime import datetime, timezone
from typing import Dict

import discord
from discord import app_commands
from discord.app_commands import Choice

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1149113323200200825  # <-- CHANGE if needed

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

MAX_PARTY_SIZE = 9

active_parties: Dict[int, dict] = {}
user_party_map: Dict[int, int] = {}
party_counter = 100


# ==============================
# Utilities
# ==============================

def parse_utc_time(time_str: str):
    if not re.match(r"^\d{2}:\d{2}$", time_str):
        return None
    hour, minute = map(int, time_str.split(":"))
    if hour > 23 or minute > 59:
        return None
    now = datetime.now(timezone.utc)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def party_total_slots(party):
    return sum(party["roles_required"].values()) + 1


def party_current_count(party):
    return len(party["members"])


def build_embed(party):
    embed = discord.Embed(
        title=f"Party #{party['id']} – {party['zone']}",
        color=discord.Color.green()
    )

    embed.add_field(
        name="Start Time",
        value=party["start_time"].strftime("%H:%M UTC"),
        inline=False
    )

    embed.add_field(
        name="Leader",
        value=f"<@{party['leader_id']}> ({party['leader_class']})",
        inline=False
    )

    roles_text = ""
    for role, count in party["roles_required"].items():
        filled = sum(1 for r in party["members"].values() if r == role)
        roles_text += f"{role.upper()} ({filled}/{count})\n"

    embed.add_field(name="Roles", value=roles_text or "None", inline=False)
    embed.add_field(
        name="Total",
        value=f"{party_current_count(party)}/{party_total_slots(party)}",
        inline=True
    )

    return embed


# ==============================
# Buttons
# ==============================

class PartyView(discord.ui.View):
    def __init__(self, party_id):
        super().__init__(timeout=None)
        self.party_id = party_id

        party = active_parties.get(party_id)
        if not party:
            return

        for role in party["roles_required"]:
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
        party = active_parties.get(self.party_id)
        if not party:
            await interaction.response.send_message(
                "Party no longer exists.",
                ephemeral=True
            )
            return

        if interaction.user.id in user_party_map:
            await interaction.response.send_message(
                "You are already in a party.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        party["members"][interaction.user.id] = self.role
        user_party_map[interaction.user.id] = self.party_id

        embed = build_embed(party)
        view = PartyView(self.party_id)

        await interaction.message.edit(embed=embed, view=view)

class LeaveButton(discord.ui.Button):
    def __init__(self, party_id):
        super().__init__(label="Leave Party", style=discord.ButtonStyle.secondary)
        self.party_id = party_id

    async def callback(self, interaction: discord.Interaction):

    party = active_parties.get(self.party_id)
    if not party:
        await interaction.response.send_message(
            "Party not found.",
            ephemeral=True
        )
        return

    if interaction.user.id not in user_party_map:
        await interaction.response.send_message(
            "You are not in this party.",
            ephemeral=True
        )
        return

    # Acknowledge interaction immediately
    await interaction.response.defer()

    # Leader leaving closes party
    if interaction.user.id == party["leader_id"]:
        await interaction.message.delete()
        del active_parties[self.party_id]
        for uid in list(user_party_map):
            if user_party_map[uid] == self.party_id:
                del user_party_map[uid]
        return

    del party["members"][interaction.user.id]
    del user_party_map[interaction.user.id]

    embed = build_embed(party)
    view = PartyView(self.party_id)

    await interaction.message.edit(embed=embed, view=view)

        party = active_parties.get(self.party_id)

        # Leader leaving closes party
        if interaction.user.id == party["leader_id"]:
            await close_party(self.party_id, interaction)
            return

        del party["members"][interaction.user.id]
        del user_party_map[interaction.user.id]

        embed = build_embed(party)
        view = PartyView(self.party_id)
        await interaction.message.edit(embed=embed, view=view)

        await interaction.response.send_message(
            "You left the party.",
            ephemeral=True
        )


class CloseButton(discord.ui.Button):
    def __init__(self, party_id):
        super().__init__(label="Close Party", style=discord.ButtonStyle.danger)
        self.party_id = party_id

    async def callback(self, interaction: discord.Interaction):

    party = active_parties.get(self.party_id)

    if not party or interaction.user.id != party["leader_id"]:
        await interaction.response.send_message(
            "Only leader can close the party.",
            ephemeral=True
        )
        return

    await interaction.response.defer()

    await interaction.message.delete()

    del active_parties[self.party_id]
    for uid in list(user_party_map):
        if user_party_map[uid] == self.party_id:
            del user_party_map[uid]

        await close_party(self.party_id, interaction)


async def close_party(party_id, interaction):
    party = active_parties.get(party_id)
    if not party:
        return

    del active_parties[party_id]
    for uid in list(user_party_map):
        if user_party_map[uid] == party_id:
            del user_party_map[uid]

    await interaction.message.delete()


# ==============================
# Slash Command
# ==============================

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

    # Support classes
    wc: int = 0,
    pp: int = 0,
    bd: int = 0,
    sws: int = 0,
    se: int = 0,
    ee: int = 0,
    bs: int = 0,

    # Damage / misc
    dd: int = 0,
    spoil: int = 0,
    leacher: int = 0,
    random: int = 0
):

    if interaction.user.id in user_party_map:
        await interaction.response.send_message(
            "You are already in a party.",
            ephemeral=True
        )
        return

    start_time = parse_utc_time(time)
    if not start_time:
        await interaction.response.send_message(
            "Invalid time format. Use HH:MM UTC.",
            ephemeral=True
        )
        return

    roles_required = {
    k: v for k, v in {
        "wc": wc,
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

    if sum(roles_required.values()) + 1 > MAX_PARTY_SIZE:
        await interaction.response.send_message(
            "Party exceeds 9 member limit.",
            ephemeral=True
        )
        return

    global party_counter
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
    }

    active_parties[party_id] = party
    user_party_map[interaction.user.id] = party_id

    embed = build_embed(party)
    view = PartyView(party_id)

    await interaction.response.send_message(embed=embed, view=view)


@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    print(f"Logged in as {bot.user}")


bot.run(TOKEN)
