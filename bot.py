import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict

import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

SPECIFIC_CLASSES = ["bd", "sws", "wc", "pp", "se", "ee", "bishop", "destro"]
SPECIAL_ROLES = ["dd", "spoil", "leacher", "random"]
ALL_ROLES = SPECIFIC_CLASSES + SPECIAL_ROLES

MAX_PARTY_SIZE = 9

active_parties: Dict[int, dict] = {}
user_party_map: Dict[int, int] = {}
party_counter = 100


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
        if sum(1 for r in members.values() if r == requested_role) < roles[requested_role]:
            return requested_role

    if requested_role == "spoil":
        if "dd" in roles:
            if sum(1 for r in members.values() if r == "dd") < roles["dd"]:
                return "dd"

    if requested_role in SPECIFIC_CLASSES:
        if "dd" in roles:
            if sum(1 for r in members.values() if r == "dd") < roles["dd"]:
                return "dd"

    if "random" in roles:
        if sum(1 for r in members.values() if r == "random") < roles["random"]:
            return "random"

    return None


def build_embed(party, guild):
    embed = discord.Embed(
        title=f"Party #{party['id']} – {party['zone']}",
        color=discord.Color.green() if party["status"] == "open" else discord.Color.red()
    )

    embed.add_field(name="Start Time", value=party["start_time"].strftime("%H:%M UTC"), inline=False)

    leader = guild.get_member(party["leader_id"])
    embed.add_field(name="Leader", value=f"{leader.mention} ({party['leader_class']})", inline=False)

    roles_text = ""
    for role, count in party["roles_required"].items():
        filled = sum(1 for r in party["members"].values() if r == role)
        roles_text += f"{role.upper()} ({filled}/{count})\n"

    embed.add_field(name="Roles", value=roles_text or "None", inline=False)
    embed.add_field(name="Total", value=f"{party_current_count(party)}/{party_total_slots(party)}", inline=True)
    embed.add_field(name="Status", value=party["status"].upper(), inline=True)

    return embed


class PartyView(discord.ui.View):
    def __init__(self, party_id):
        super().__init__(timeout=None)
        self.party_id = party_id

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id in user_party_map:
            await interaction.response.send_message("You are already in a party.", ephemeral=True)
            return False
        return True

    async def join_role(self, interaction: discord.Interaction, role: str):
        party = active_parties.get(self.party_id)
        if not party:
            await interaction.response.send_message("Party not found.", ephemeral=True)
            return

        if is_party_full(party):
            await interaction.response.send_message("Party is full.", ephemeral=True)
            return

        assigned = try_assign_role(party, role)

        if not assigned:
            await interaction.response.send_message("No available slot.", ephemeral=True)
            return

        party["members"][interaction.user.id] = assigned
        user_party_map[interaction.user.id] = self.party_id

        await update_party_message(party, interaction.guild)
        await interaction.response.send_message("Joined successfully.", ephemeral=True)

    def generate_buttons(self):
        for role in active_parties[self.party_id]["roles_required"]:
            button = discord.ui.Button(label=f"Join {role.upper()}", style=discord.ButtonStyle.primary)

            async def callback(interaction, r=role):
                await self.join_role(interaction, r)

            button.callback = callback
            self.add_item(button)


async def update_party_message(party, guild):
    channel = guild.get_channel(party["channel_id"])
    message = await channel.fetch_message(party["message_id"])
    embed = build_embed(party, guild)
    view = PartyView(party["id"])
    view.generate_buttons()
    await message.edit(embed=embed, view=view)


@tree.command(name="lfp", description="Create party")
async def lfp(interaction: discord.Interaction, zone: str, time: str, leader_class: str, dd: int = 0, spoil: int = 0, leacher: int = 0, random: int = 0):
    global party_counter

    if interaction.user.id in user_party_map:
        await interaction.response.send_message("You are already in a party.", ephemeral=True)
        return

    start_time = parse_utc_time(time)
    if not start_time:
        await interaction.response.send_message("Invalid time format. Use HH:MM UTC.", ephemeral=True)
        return

    roles_required = {k: v for k, v in {"dd": dd, "spoil": spoil, "leacher": leacher, "random": random}.items() if v > 0}

    total = sum(roles_required.values()) + 1
    if total > MAX_PARTY_SIZE:
        await interaction.response.send_message("Party exceeds 9 member limit.", ephemeral=True)
        return

    party_counter += 1
    party_id = party_counter

    party = {
        "id": party_id,
        "zone": zone,
        "leader_id": interaction.user.id,
        "leader_class": leader_class,
        "start_time": start_time,
        "roles_required": roles_required,
        "members": {interaction.user.id: leader_class},
        "status": "open",
        "channel_id": interaction.channel.id,
        "message_id": None
    }

    active_parties[party_id] = party
    user_party_map[interaction.user.id] = party_id

    embed = build_embed(party, interaction.guild)
    view = PartyView(party_id)
    view.generate_buttons()

    await interaction.response.send_message(embed=embed, view=view)
    message = await interaction.original_response()
    party["message_id"] = message.id


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await tree.sync()


bot.run(TOKEN)
