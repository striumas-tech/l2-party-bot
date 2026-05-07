import asyncpg
import os
import json
import uuid
import asyncio
from zoneinfo import ZoneInfo, available_timezones
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.app_commands import Choice

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

intents = discord.Intents.default()
intents.members = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

db_pool = None
scheduler_started = False
ALL_TIMEZONES = sorted(available_timezones())

ROLE_DATA = {
    "tank": {"icon": "🛡", "name": "Tank"},
    "leecher": {"icon": "🧟", "name": "Leecher"},
    "random": {"icon": "🎲", "name": "Random"},
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

# ================= DATABASE =================

async def setup_database():
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_timezones (
                user_id BIGINT PRIMARY KEY,
                timezone TEXT NOT NULL
            );
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id BIGINT PRIMARY KEY,
                party_channel_id BIGINT
            );
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS lfp_parties (
                party_id TEXT PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                message_id BIGINT,
                leader_id BIGINT NOT NULL,
                leader_class TEXT NOT NULL,
                zone TEXT NOT NULL,
                roles_required JSONB NOT NULL,
                start_time TIMESTAMPTZ NOT NULL,
                end_time TIMESTAMPTZ NOT NULL,
                reminded BOOLEAN DEFAULT FALSE
            );
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS lfp_party_members (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                party_id TEXT NOT NULL REFERENCES lfp_parties(party_id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
        """)


async def load_party(party_id: str):
    async with db_pool.acquire() as conn:
        party_row = await conn.fetchrow(
            "SELECT * FROM lfp_parties WHERE party_id=$1",
            party_id
        )

        if not party_row:
            return None

        member_rows = await conn.fetch(
            "SELECT user_id, role FROM lfp_party_members WHERE party_id=$1",
            party_id
        )

    roles_required = party_row["roles_required"]
    if isinstance(roles_required, str):
        roles_required = json.loads(roles_required)

    guild = bot.get_guild(party_row["guild_id"])

    return {
        "guild": guild,
        "guild_id": party_row["guild_id"],
        "zone": party_row["zone"],
        "party_id": party_row["party_id"],
        "leader_id": party_row["leader_id"],
        "leader_class": party_row["leader_class"],
        "start_time": party_row["start_time"],
        "end_time": party_row["end_time"],
        "roles_required": roles_required,
        "members": {row["user_id"]: row["role"] for row in member_rows},
        "channel_id": party_row["channel_id"],
        "message_id": party_row["message_id"],
        "reminded": party_row["reminded"],
    }


async def delete_party(party_id: str):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM lfp_parties WHERE party_id=$1", party_id)


async def get_party_channel_id(guild_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT party_channel_id FROM guild_config WHERE guild_id=$1",
            guild_id
        )

    if not row:
        return None

    return row["party_channel_id"]


# ================= UTILITIES =================

async def parse_user_time(time_str: str, interaction: discord.Interaction):
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError("Time must be in HH:MM format.")

    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        raise ValueError("Time must contain only numbers, for example 19:30.")

    async with db_pool.acquire() as conn:
        tz_row = await conn.fetchrow(
            "SELECT timezone FROM user_timezones WHERE user_id=$1",
            interaction.user.id
        )

    if not tz_row:
        raise ValueError("Set your timezone first with /settimezone.")

    user_tz = ZoneInfo(tz_row["timezone"])
    now_local = datetime.now(user_tz)

    if hour == 24 and minute == 0:
        local_time = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        return local_time.astimezone(timezone.utc)

    if not (0 <= hour <= 23):
        raise ValueError("Hour must be between 00 and 23.")
    if not (0 <= minute <= 59):
        raise ValueError("Minute must be between 00 and 59.")

    local_time = now_local.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0
    )

    if local_time <= now_local:
        local_time += timedelta(days=1)

    return local_time.astimezone(timezone.utc)


def generate_party_id(zone: str):
    return f"{zone.upper()}-{uuid.uuid4().hex[:6].upper()}"


def progress_bar(current, total, length=14):
    if total <= 0:
        return "░" * length

    filled = int(length * current / total)
    return "█" * filled + "░" * (length - filled)


def build_embed(party):
    now = datetime.now(timezone.utc)

    start_ts = int(party["start_time"].timestamp())
    end_ts = int(party["end_time"].timestamp())

    total = sum(party["roles_required"].values())
    current = len(party["members"])

    if current >= total:
        status = "🟣 FULL"
        color = discord.Color.purple()
    elif now >= party["end_time"]:
        status = "⚫ ENDED"
        color = discord.Color.dark_gray()
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
        name="⏱ PARTY TIME",
        value=(
            f"**Start:** <t:{start_ts}:t> (<t:{start_ts}:R>)\n"
            f"**End:** <t:{end_ts}:t> (<t:{end_ts}:R>)"
        ),
        inline=False
    )

    guild = party["guild"]
    leader_member = guild.get_member(party["leader_id"]) if guild else None
    leader_name = leader_member.display_name if leader_member else f"<@{party['leader_id']}>"

    embed.add_field(
        name="👑 LEADER",
        value=f"{leader_name} • {ROLE_DATA[party['leader_class']]['name']}",
        inline=False
    )

    groups = {
        "🛡 TANK": ["tank"],
        "🧩 SUPPORT": ["wc", "pp", "bd", "sws", "se", "ee", "bs"],
        "⚔️ DPS": ["dd", "mage", "sum", "spoil"],
        "🎯 OTHER": ["leecher", "random"],
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

                section_text += (
                    f"{mark} {ROLE_DATA[role]['icon']} "
                    f"**{ROLE_DATA[role]['name']}** `{filled}/{required}`\n"
                )

                for uid in role_members:
                    crown = " 👑" if uid == party["leader_id"] else ""
                    section_text += f" • <@{uid}>{crown}\n"

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
    def __init__(self, party_id, party=None, viewer_id=None):
        super().__init__(timeout=None)

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
        super().__init__(
            label=f"Join {ROLE_DATA[role]['name']}",
            style=discord.ButtonStyle.primary,
            custom_id=f"join:{party_id}:{role}"
        )
        self.party_id = party_id
        self.role = role

    async def callback(self, interaction: discord.Interaction):
        party = await load_party(self.party_id)

        if not party:
            await interaction.response.send_message(
                "This party is no longer active. Please create a new one.",
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    """
                    SELECT party_id FROM lfp_party_members
                    WHERE guild_id=$1 AND user_id=$2
                    """,
                    guild_id,
                    interaction.user.id
                )

                if existing:
                    await interaction.response.send_message(
                        "Already in party.",
                        ephemeral=True
                    )
                    return

                filled = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM lfp_party_members
                    WHERE party_id=$1 AND role=$2
                    """,
                    self.party_id,
                    self.role
                )

                required = party["roles_required"].get(self.role, 0)

                if filled >= required:
                    await interaction.response.send_message(
                        "That role is already full.",
                        ephemeral=True
                    )
                    return

                await conn.execute(
                    """
                    INSERT INTO lfp_party_members (guild_id, user_id, party_id, role)
                    VALUES ($1,$2,$3,$4)
                    """,
                    guild_id,
                    interaction.user.id,
                    self.party_id,
                    self.role
                )

        await interaction.response.defer()

        party = await load_party(self.party_id)
        await interaction.message.edit(
            embed=build_embed(party),
            view=PartyView(self.party_id, party, interaction.user.id)
        )


class LeaveButton(discord.ui.Button):
    def __init__(self, party_id):
        super().__init__(
            label="Leave",
            style=discord.ButtonStyle.secondary,
            custom_id=f"leave:{party_id}"
        )
        self.party_id = party_id

    async def callback(self, interaction: discord.Interaction):
        party = await load_party(self.party_id)

        if not party:
            await interaction.response.send_message(
                "This party is no longer active. Please create a new one.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        if interaction.user.id == party["leader_id"]:
            try:
                await interaction.message.delete()
            except Exception:
                pass

            await delete_party(self.party_id)
            return

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM lfp_party_members
                WHERE guild_id=$1 AND user_id=$2 AND party_id=$3
                """,
                interaction.guild.id,
                interaction.user.id,
                self.party_id
            )

        party = await load_party(self.party_id)

        if party:
            await interaction.message.edit(
                embed=build_embed(party),
                view=PartyView(self.party_id, party, interaction.user.id)
            )


class CancelButton(discord.ui.Button):
    def __init__(self, party_id):
        super().__init__(
            label="Cancel Party",
            style=discord.ButtonStyle.danger,
            custom_id=f"cancel:{party_id}"
        )
        self.party_id = party_id

    async def callback(self, interaction: discord.Interaction):
        party = await load_party(self.party_id)

        if not party:
            await interaction.response.send_message(
                "This party is no longer active. Please create a new one.",
                ephemeral=True
            )
            return

        if interaction.user.id != party["leader_id"]:
            await interaction.response.send_message(
                "Only party leader can cancel.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            await interaction.message.delete()
        except Exception:
            pass

        await delete_party(self.party_id)


# ================= AUTOCOMPLETE =================

async def timezone_autocomplete(interaction: discord.Interaction, current: str):
    matches = [
        tz for tz in ALL_TIMEZONES
        if current.lower() in tz.lower()
    ]

    return [
        app_commands.Choice(name=tz, value=tz)
        for tz in matches[:25]
    ]


# ================= COMMANDS =================

@tree.command(name="setpartychannel", description="Set party channel for this server")
@app_commands.default_permissions(manage_guild=True)
async def setpartychannel(interaction: discord.Interaction, channel: discord.TextChannel):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO guild_config (guild_id, party_channel_id)
            VALUES ($1,$2)
            ON CONFLICT (guild_id)
            DO UPDATE SET party_channel_id=EXCLUDED.party_channel_id
            """,
            interaction.guild.id,
            channel.id
        )

    await interaction.response.send_message(
        f"✅ Party channel set to {channel.mention}",
        ephemeral=True
    )


@tree.command(name="settimezone", description="Set your timezone")
@app_commands.autocomplete(timezone=timezone_autocomplete)
async def settimezone(interaction: discord.Interaction, timezone: str):
    try:
        ZoneInfo(timezone)
    except Exception:
        await interaction.response.send_message(
            "Invalid timezone selected.",
            ephemeral=True
        )
        return

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_timezones (user_id, timezone)
            VALUES ($1,$2)
            ON CONFLICT (user_id)
            DO UPDATE SET timezone=$2
            """,
            interaction.user.id,
            timezone
        )

    await interaction.response.send_message(
        f"✅ Timezone set to **{timezone}**",
        ephemeral=True
    )


@tree.command(name="lfp", description="Create party")
@app_commands.choices(
    leader_class=[Choice(name=v["name"], value=k) for k, v in ROLE_DATA.items()]
)
async def lfp(
    interaction: discord.Interaction,
    zone: str,
    start: str,
    end: str,
    leader_class: Choice[str],
    tank: int = 0,
    wc: int = 0,
    pp: int = 0,
    bd: int = 0,
    leecher: int = 0,
    random: int = 0,
    sws: int = 0,
    se: int = 0,
    ee: int = 0,
    bs: int = 0,
    dd: int = 0,
    mage: int = 0,
    sum: int = 0,
    spoil: int = 0,
):
    try:
        start_time = await parse_user_time(start, interaction)
        end_time = await parse_user_time(end, interaction)
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    if end_time <= start_time:
        await interaction.response.send_message(
            "End time must be after start time.",
            ephemeral=True
        )
        return

    guild_id = interaction.guild.id

    target_channel_id = await get_party_channel_id(guild_id)
    target_channel = bot.get_channel(target_channel_id) if target_channel_id else interaction.channel

    if not target_channel:
        await interaction.response.send_message(
            "Party channel not found. Use /setpartychannel again.",
            ephemeral=True
        )
        return

    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            """
            SELECT party_id FROM lfp_party_members
            WHERE guild_id=$1 AND user_id=$2
            """,
            guild_id,
            interaction.user.id
        )

    if existing:
        await interaction.response.send_message(
            "You are already in a party in this server.",
            ephemeral=True
        )
        return

    roles_input = {
        "tank": tank,
        "wc": wc, "pp": pp, "bd": bd, "sws": sws,
        "se": se, "ee": ee, "bs": bs,
        "dd": dd, "mage": mage, "sum": sum, "spoil": spoil,
        "leecher": leecher,
        "random": random,
    }

    roles_required = {k: v for k, v in roles_input.items() if v > 0}

    if leader_class.value not in roles_required:
        roles_required[leader_class.value] = 1

    party_id = generate_party_id(zone)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO lfp_parties (
                party_id, guild_id, channel_id, message_id,
                leader_id, leader_class, zone,
                roles_required, start_time, end_time, reminded
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
            party_id,
            guild_id,
            target_channel.id,
            None,
            interaction.user.id,
            leader_class.value,
            zone,
            json.dumps(roles_required),
            start_time,
            end_time,
            False
        )

        await conn.execute(
            """
            INSERT INTO lfp_party_members (guild_id, user_id, party_id, role)
            VALUES ($1,$2,$3,$4)
            """,
            guild_id,
            interaction.user.id,
            party_id,
            leader_class.value
        )

    party = await load_party(party_id)

    sent = await target_channel.send(
        embed=build_embed(party),
        view=PartyView(party_id, party, interaction.user.id)
    )

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE lfp_parties SET message_id=$1 WHERE party_id=$2",
            sent.id,
            party_id
        )

    await interaction.response.send_message(
        f"✅ Party created in {target_channel.mention}",
        ephemeral=True
    )


# ================= SCHEDULER =================

async def party_scheduler():
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = datetime.now(timezone.utc)

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT party_id FROM lfp_parties")

        for row in rows:
            party = await load_party(row["party_id"])

            if not party:
                continue

            channel = bot.get_channel(party["channel_id"])
            if not channel:
                continue

            if not party["reminded"]:
                seconds_left = (party["start_time"] - now).total_seconds()

                if 0 < seconds_left <= 600:
                    mentions = " ".join(f"<@{uid}>" for uid in party["members"])

                    await channel.send(
                        f"⏰ **{party['zone'].upper()} PARTY starts in 10 minutes!**\n{mentions}",
                        allowed_mentions=discord.AllowedMentions(users=True)
                    )

                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE lfp_parties SET reminded=TRUE WHERE party_id=$1",
                            party["party_id"]
                        )

            if now >= party["end_time"]:
                try:
                    msg = await channel.fetch_message(party["message_id"])
                    await msg.delete()
                except Exception:
                    pass

                await delete_party(party["party_id"])

                await channel.send(
                    f"❌ **{party['zone'].upper()} PARTY expired.**"
                )

                continue

            try:
                msg = await channel.fetch_message(party["message_id"])
                await msg.edit(
                    embed=build_embed(party),
                    view=PartyView(party["party_id"], party, party["leader_id"])
                )
            except Exception:
                pass

        await asyncio.sleep(60)


# ================= READY =================

@bot.event
async def on_ready():
    global db_pool, scheduler_started

    if db_pool is None:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            ssl="require"
        )

        await setup_database()

    if not scheduler_started:

        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT party_id FROM lfp_parties"
            )

        for row in rows:
            party = await load_party(row["party_id"])

            if party:
                bot.add_view(
                    PartyView(party["party_id"], party)
                )

        bot.loop.create_task(
            party_scheduler()
        )

        scheduler_started = True

    # THIS MUST BE INSIDE on_ready()
    print("Connected guilds:")

    for guild in bot.guilds:
        print(f"{guild.name} ({guild.id})")

    try:
        guild_obj = discord.Object(id=guild.id)

        tree.copy_global_to(guild=guild_obj)
        synced = await tree.sync(guild=guild_obj)

        print(f"Synced {len(synced)} commands to {guild.name}")

    except Exception as e:
        print(f"Failed sync for {guild.name}: {e}")

    print(
        f"Logged in as {bot.user} in {len(bot.guilds)} servers"
    )


bot.run(TOKEN)
