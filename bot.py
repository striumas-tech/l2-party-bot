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
GUILD_ID = 1149113323200200825  # your server ID

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

MAX_PARTY_SIZE = 9

active_parties: Dict[int, dict] = {}
user_party_map: Dict[int, int] = {}
button_cooldowns: Dict[int, float] = {}

party_counter = 100


# =========================
# Utility
# =========================

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


from datetime import datetime, timezone

def progress_bar(current, total, length=12):
    filled = int(length * current / total) if total > 0 else 0
    return "█" * filled + "░" * (length - filled)

def build_embed(party):
    now = datetime.now(timezone.utc)
    start_ts = int(party["start_time"].timestamp())

    total_slots = sum(party["roles_required"].values()) + 1
    current_members = len(party["members"])

    # ===== STATUS SYSTEM =====
    if current_members >= total_slots:
        status = "🟣 FULL"
        color = discord.Color.purple()
    elif now >= party["start_time"]:
        status = "🔴 STARTED"
        color = discord.Color.red()
    elif (party["start_time"] - now).total_seconds() <= 600:
        status = "🟠 FORMING (SOON)"
        color = discord.Color.orange()
    else:
        status = "🟢 RECRUITING"
        color = discord.Color.green()

    embed = discord.Embed(
        title=f"⚔️ {party['zone'].upper()} RAID LOBBY",
        color=color
    )

    # ===== TIME BLOCK =====
    embed.add_field(
        name="⏱ RAID TIMER",
        value=f"🕒 **<t:{start_ts}:t>**\n⏳ <t:{start_ts}:R>",
        inline=False
    )

    # ===== LEADER BLOCK =====
    embed.add_field(
        name="👑 RAID LEADER",
        value=f"<@{party['leader_id']}>  •  **{party['leader_class'].upper()}**",
        inline=False
    )

    # ===== ROLE SECTION =====
    support_roles = ["wc", "pp", "bd", "sws", "se", "ee", "bs"]
    dps_roles = ["destro", "dd", "spoil"]
    misc_roles = ["leacher", "random"]

    def role_section(role_list, title):
        text = ""
        for role in role_list:
            if role in party["roles_required"]:
                required = party["roles_required"][role]
                filled = sum(1 for r in party["members"].values() if r == role)
                mark = "✔️" if filled >= required else "❌"
                text += f"{mark} **{role.upper():<8}** `{filled}/{required}`\n"
        return text if text else "—"

    embed.add_field(
        name="🛡 SUPPORT",
        value=role_section(support_roles, "SUPPORT"),
        inline=True
    )

    embed.add_field(
        name="⚔️ DPS",
        value=role_section(dps_roles, "DPS"),
        inline=True
    )

    embed.add_field(
        name="🎲 OTHER",
        value=role_section(misc_roles, "OTHER"),
        inline=True
    )

    # ===== PROGRESS =====
    embed.add_field(
        name="📊 RAID CAPACITY",
        value=f"`{progress_bar(current_members, total_slots)}`\n**{current_members}/{total_slots} Members**",
        inline=False
    )

    embed.add_field(
        name="📌 STATUS",
        value=f"**{status}**",
        inline=False
    )

    embed.set_footer(text="Lineage II Hardcore Raid System • UTC Based")

    return embed

def check_cooldown(user_id: int):
    now = time.time()
    last = button_cooldowns.get(user_id, 0)
    if now - last < 2:
        return False
    button_cooldowns[user_id] = now
    return True


# =========================
# Buttons
# =========================

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

        if not check_cooldown(interaction.user.id):
            await interaction.response.send_message("Slow down (2s cooldown).", ephemeral=True)
            return

        party = active_parties.get(self.party_id)
        if not party:
            await interaction.response.send_message("Party no longer exists.", ephemeral=True)
            return

        if interaction.user.id in user_party_map:
            await interaction.response.send_message("You are already in a party.", ephemeral=True)
            return

        await interaction.response.defer()

        party["members"][interaction.user.id] = self.role
        user_party_map[interaction.user.id] = self.party_id

        embed = build_embed(party)
        view = PartyView(self.party_id)
        await interaction.message.edit(embed=embed, view=view)


class LeaveButton(discord.ui.Button):
    def __init__(self, party_id):
        super().__init__(label="Leave", style=discord.ButtonStyle.secondary)
        self.party_id = party_id

    async def callback(self, interaction: discord.Interaction):

        if not check_cooldown(interaction.user.id):
            await interaction.response.send_message("Slow down (2s cooldown).", ephemeral=True)
            return

        party = active_parties.get(self.party_id)
        if not party:
            await interaction.response.send_message("Party not found.", ephemeral=True)
            return

        if interaction.user.id not in user_party_map:
            await interaction.response.send_message("You are not in this party.", ephemeral=True)
            return

        await interaction.response.defer()

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


class CloseButton(discord.ui.Button):
    def __init__(self, party_id):
        super().__init__(label="Close", style=discord.ButtonStyle.danger)
        self.party_id = party_id

    async def callback(self, interaction: discord.Interaction):

        if not check_cooldown(interaction.user.id):
            await interaction.response.send_message("Slow down (2s cooldown).", ephemeral=True)
            return

        party = active_parties.get(self.party_id)
        if not party or interaction.user.id != party["leader_id"]:
            await interaction.response.send_message("Only leader can close.", ephemeral=True)
            return

        await interaction.response.defer()

        await interaction.message.delete()
        del active_parties[self.party_id]
        for uid in list(user_party_map):
            if user_party_map[uid] == self.party_id:
                del user_party_map[uid]


# =========================
# Slash Command
# =========================

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

    if interaction.user.id in user_party_map:
        await interaction.response.send_message("You are already in a party.", ephemeral=True)
        return

    start_time = parse_utc_time(time)
    if not start_time:
        await interaction.response.send_message("Invalid time format (HH:MM UTC).", ephemeral=True)
        return

    roles_required = {
        k: v for k, v in {
            "wc": wc,
            "pp": pp,
            "destro": destro,
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

    total_requested = sum(roles_required.values())

# Leader occupies one slot
total_members = total_requested + 1

# If leader role is already in requested roles, reduce one slot
if leader_class.value in roles_required:
    total_members = total_requested  # leader fills one of requested slots

if total_members > MAX_PARTY_SIZE:
    await interaction.response.send_message(
        "Party exceeds 9 members.",
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
        "message_id": None,
        "channel_id": interaction.channel.id,
        "reminded": False
    }

    active_parties[party_id] = party
    user_party_map[interaction.user.id] = party_id

    embed = build_embed(party)
    view = PartyView(party_id)

    await interaction.response.send_message(embed=embed, view=view)

    sent = await interaction.original_response()
    party["message_id"] = sent.id


# =========================
# Background Scheduler
# =========================

async def party_scheduler():
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = datetime.now(timezone.utc)

        for party_id in list(active_parties.keys()):
            party = active_parties.get(party_id)
            if not party:
                continue

            channel = bot.get_channel(party["channel_id"])
            if not channel:
                continue

            start = party["start_time"]

            # 10 minute reminder
            if not party["reminded"] and 0 < (start - now).total_seconds() <= 600:
                mentions = " ".join([f"<@{uid}>" for uid in party["members"]])
                await channel.send(f"⏰ Party starts in 10 minutes!\n{mentions}")
                party["reminded"] = True

            # 30 minute expire if not full
            if now > start and (now - start).total_seconds() >= 1800:
                if party_current_count(party) < party_total_slots(party):
                    try:
                        msg = await channel.fetch_message(party["message_id"])
                        await msg.delete()
                    except:
                        pass

                    del active_parties[party_id]
                    for uid in list(user_party_map):
                        if user_party_map[uid] == party_id:
                            del user_party_map[uid]

        await asyncio.sleep(60)


@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    bot.loop.create_task(party_scheduler())
    print(f"Logged in as {bot.user}")


bot.run(TOKEN)
