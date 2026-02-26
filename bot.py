import os
import re
from datetime import datetime, timezone
from typing import Dict

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True  # REQUIRED for prefix commands

bot = commands.Bot(command_prefix=".", intents=intents)

MAX_PARTY_SIZE = 9

active_parties: Dict[int, dict] = {}
user_party_map: Dict[int, int] = {}
party_counter = 100


# ==============================
# Utility Functions
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


def is_party_full(party):
    return party_current_count(party) >= party_total_slots(party)


def try_assign_role(party, requested_role):
    roles = party["roles_required"]
    members = party["members"]

    if requested_role in roles:
        filled = sum(1 for r in members.values() if r == requested_role)
        if filled < roles[requested_role]:
            return requested_role

    if requested_role == "spoil" and "dd" in roles:
        filled = sum(1 for r in members.values() if r == "dd")
        if filled < roles["dd"]:
            return "dd"

    if "random" in roles:
        filled = sum(1 for r in members.values() if r == "random")
        if filled < roles["random"]:
            return "random"

    return None


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
# Button System
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


class JoinButton(discord.ui.Button):
    def __init__(self, party_id, role):
        super().__init__(
            label=f"Join {role.upper()}",
            style=discord.ButtonStyle.primary
        )
        self.party_id = party_id
        self.role = role

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id in user_party_map:
            await interaction.response.send_message(
                "You are already in a party.",
                ephemeral=True
            )
            return

        party = active_parties.get(self.party_id)
        if not party:
            await interaction.response.send_message(
                "Party no longer exists.",
                ephemeral=True
            )
            return

        if is_party_full(party):
            await interaction.response.send_message(
                "Party is full.",
                ephemeral=True
            )
            return

        assigned = try_assign_role(party, self.role)
        if not assigned:
            await interaction.response.send_message(
                "No available slot.",
                ephemeral=True
            )
            return

        party["members"][interaction.user.id] = assigned
        user_party_map[interaction.user.id] = self.party_id

        await update_party_message(party)
        await interaction.response.send_message(
            "Joined successfully.",
            ephemeral=True
        )


async def update_party_message(party):
    channel = bot.get_channel(party["channel_id"])
    message = await channel.fetch_message(party["message_id"])
    embed = build_embed(party)
    view = PartyView(party["id"])
    await message.edit(embed=embed, view=view)


# ==============================
# Commands
# ==============================

@bot.command()
async def lfp(ctx, zone: str, time: str, leader_class: str,
              dd: int = 0, spoil: int = 0, leacher: int = 0, random: int = 0):

    if ctx.author.id in user_party_map:
        await ctx.send("You are already in a party.")
        return

    start_time = parse_utc_time(time)
    if not start_time:
        await ctx.send("Invalid time format. Use HH:MM UTC.")
        return

    roles_required = {
        k: v for k, v in {
            "dd": dd,
            "spoil": spoil,
            "leacher": leacher,
            "random": random
        }.items() if v > 0
    }

    total = sum(roles_required.values()) + 1
    if total > MAX_PARTY_SIZE:
        await ctx.send("Party exceeds 9 member limit.")
        return

    global party_counter
    party_counter += 1
    party_id = party_counter

    party = {
        "id": party_id,
        "zone": zone,
        "leader_id": ctx.author.id,
        "leader_class": leader_class,
        "start_time": start_time,
        "roles_required": roles_required,
        "members": {ctx.author.id: leader_class},
        "channel_id": ctx.channel.id,
        "message_id": None
    }

    active_parties[party_id] = party
    user_party_map[ctx.author.id] = party_id

    embed = build_embed(party)
    view = PartyView(party_id)

    message = await ctx.send(embed=embed, view=view)
    party["message_id"] = message.id


@bot.command()
async def leave(ctx):
    if ctx.author.id not in user_party_map:
        await ctx.send("You are not in a party.")
        return

    party_id = user_party_map[ctx.author.id]
    party = active_parties.get(party_id)

    if ctx.author.id == party["leader_id"]:
        del active_parties[party_id]
        for uid in list(user_party_map):
            if user_party_map[uid] == party_id:
                del user_party_map[uid]
        await ctx.send("Leader left. Party closed.")
        return

    del party["members"][ctx.author.id]
    del user_party_map[ctx.author.id]

    await update_party_message(party)
    await ctx.send("You left the party.")


@bot.command()
async def close(ctx):
    if ctx.author.id not in user_party_map:
        await ctx.send("You are not in a party.")
        return

    party_id = user_party_map[ctx.author.id]
    party = active_parties.get(party_id)

    if ctx.author.id != party["leader_id"]:
        await ctx.send("Only leader can close the party.")
        return

    del active_parties[party_id]
    for uid in list(user_party_map):
        if user_party_map[uid] == party_id:
            del user_party_map[uid]

    await ctx.send("Party closed.")


bot.run(TOKEN)
