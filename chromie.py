import os
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ==========================
# CONFIG
# ==========================

# Default timezone for all events
DEFAULT_TZ = ZoneInfo("America/Chicago")

# How often to update pinned countdowns (seconds)
UPDATE_INTERVAL_SECONDS = 60

# Default milestones (days before event) – messages at these offsets
DEFAULT_MILESTONES = [100, 50, 30, 14, 7, 2, 1]

# Where we store all data (per server)
DATA_FILE = Path(os.getenv("CHROMIE_DATA_PATH", "/var/data/chromie_state.json"))

# Bot token – preferred: set DISCORD_BOT_TOKEN in your hosting env
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
# For local testing only, you *could* paste your token here instead:
# if not TOKEN:
#     TOKEN = "YOUR_BOT_TOKEN_HERE"

EMBED_COLOR = discord.Color.from_rgb(140, 82, 255)  # ChronoBot purple

# ==========================
# STATE HANDLING
# ==========================

"""
State structure:

{
  "guilds": {
    "123456789012345678": {
      "event_channel_id": 987654321098765432,
      "pinned_message_id": 123123123123123123,
      "event_admin_role_ids": [111, 222],  # roles allowed to manage ANY event
      "allow_member_event_creation": false,
      "events": [
        {
          "name": "Couples Retreat 💕",
          "timestamp": 1771000800,          # unix seconds
          "owner_id": 111111111111111111,   # user id (who created / owns this event)
          "milestones": [100, 50, 30, ...],
          "announced_milestones": [100, 50],
        },
        ...
      ],
      "welcomed": true
    },
    ...
  },
  "user_links": {
    "user_id_str": guild_id_int
  }
}
"""


def load_state():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    else:
        data = {}

    data.setdefault("guilds", {})
    data.setdefault("user_links", {})
    return data


def save_state():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_guild_state(guild_id: int):
    gid = str(guild_id)
    guilds = state.setdefault("guilds", {})
    if gid not in guilds:
        guilds[gid] = {
            "event_channel_id": None,
            "pinned_message_id": None,
            "event_admin_role_ids": [],
            "allow_member_event_creation": False,
            "events": [],
            "welcomed": False,
        }
    else:
        guilds[gid].setdefault("event_channel_id", None)
        guilds[gid].setdefault("pinned_message_id", None)
        guilds[gid].setdefault("event_admin_role_ids", [])
        guilds[gid].setdefault("allow_member_event_creation", False)
        guilds[gid].setdefault("events", [])
        guilds[gid].setdefault("welcomed", False)
    return guilds[gid]


def get_user_links():
    return state.setdefault("user_links", {})


def sort_events(guild_state: dict):
    """Sort events soonest → farthest based on timestamp."""
    events = guild_state.get("events", [])
    events.sort(key=lambda ev: ev.get("timestamp", 0))
    guild_state["events"] = events


state = load_state()
for _, g_state in state.get("guilds", {}).items():
    sort_events(g_state)
    g_state.setdefault("event_admin_role_ids", [])
    g_state.setdefault("allow_member_event_creation", False)
    for ev in g_state.get("events", []):
        ev.setdefault("owner_id", None)
save_state()


# ==========================
# PERMISSION HELPERS
# ==========================

async def get_member_or_fetch(guild: discord.Guild, user_id: int) -> discord.Member | None:
    """Best-effort member lookup that works even when member cache is cold."""
    m = guild.get_member(user_id)
    if m is not None:
        return m
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def member_is_event_manager(member: discord.Member, guild_state: dict) -> bool:
    """Server-wide event managers: Manage Server, Administrator, or configured admin roles."""
    perms = getattr(member, "guild_permissions", None)
    if perms and (perms.administrator or perms.manage_guild):
        return True

    role_ids = set(guild_state.get("event_admin_role_ids", []))
    return any(r.id in role_ids for r in getattr(member, "roles", []))


async def can_edit_event(
    guild: discord.Guild,
    guild_state: dict,
    actor_id: int,
    event: dict,
) -> bool:
    """Per-event edit gate: owner OR server event manager."""
    member = await get_member_or_fetch(guild, actor_id)
    if member is None:
        return False

    if member_is_event_manager(member, guild_state):
        return True

    owner_id = event.get("owner_id")
    return owner_id is not None and int(owner_id) == int(actor_id)


# ==========================
# DISCORD SETUP
# ==========================

intents = discord.Intents.default()
# Slash commands do NOT require message_content, but having it on is fine.
bot = commands.Bot(command_prefix="None", intents=intents)


async def send_onboarding_for_guild(guild: discord.Guild):
    """Send the onboarding/setup message for a guild, and mark it welcomed once."""
    guild_state = get_guild_state(guild.id)

    # If we've already tried to welcome this guild, don't do it again automatically
    if guild_state.get("welcomed"):
        return

    contact_user = guild.owner or (await bot.fetch_user(guild.owner_id))
    setup_message = (
    f"Hi {contact_user.mention if contact_user else ''}! "
    f"Thanks for adding **ChronoBot** to **{guild.name}** 🕒💕\n\n"
    "I’m Chromie, your server’s friendly countdown bot for all your upcoming events! "
    "I’ll keep track of the big day and send reminders along the way, so no one forgets what’s coming up.\n\n"
    "I’ll announce milestones at **100 days, 50 days, about 1 month (30 days), 14 days, 1 week, 2 days, "
    "the day before, and on the day of the event**.\n\n"
    "Goodbye forgotten events, hello Chromie-powered hype.\n\n"
    "**Here’s a quick setup guide:**\n\n"
    "1️⃣ **Choose your events channel**\n"
    "   • Go to the channel where you want the live countdown pinned.\n"
    "   • Run: `/seteventchannel`\n\n"
    "2️⃣ **Add your first event (MM/DD/YYYY)**\n"
    "   • Example: `/addevent date: 04/12/2026 time: 09:00 name: Game Night  `\n"
    "   • Format: `MM/DD/YYYY` and `HH:MM` 24-hour time (server timezone).\n\n"
    "3️⃣ **Manage your events**\n"
    "   • `/listevents` – show all events\n"
    "   • `/removeevent` – remove by list number\n"
    "   • (New) Event owners can edit their own events\n"
    "   • (New) `/addeventadminrole` – allow a role to manage all events\n"
    "   • (New) `/allowmemberaddevents` – let members create their own events\n"
    "   • `/update_countdown` – refresh the pinned countdown\n\n"
    "🔁 **Optional: DM control**\n"
    "   • In this server, run `/linkserver`.\n"
    "   • Then DM me: `/addevent` with your date, time, and name.\n\n"
    "I’ll handle the live countdown and milestone reminders automatically once an "
    "events channel and at least one event are set up. ✨"
    )


    sent = False
    if contact_user:
        try:
            await contact_user.send(setup_message)
            sent = True
        except discord.Forbidden:
            sent = False

    if not sent:
        # Fallback: try system channel, then first text channel where I can speak
        fallback_channel = guild.system_channel
        if fallback_channel is None:
            for channel in guild.text_channels:
                perms = channel.permissions_for(guild.me)
                if perms.send_messages:
                    fallback_channel = channel
                    break

        if fallback_channel is not None:
            try:
                await fallback_channel.send(setup_message)
                sent = True
            except discord.Forbidden:
                sent = False

    # Mark as welcomed after the first attempt
    guild_state["welcomed"] = True
    save_state()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Sync slash commands
    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Error syncing commands: {e}")

    if not update_countdowns.is_running():
        update_countdowns.start()


@bot.event
async def on_guild_join(guild: discord.Guild):
    """When the bot is added to a new server, send the setup guide."""
    g_state = get_guild_state(guild.id)
    sort_events(g_state)
    save_state()
    await send_onboarding_for_guild(guild)


# ==========================
# TIME & EMBED HELPERS
# ==========================

def compute_time_left(dt: datetime):
    """Return (description, days_left, event_passed)."""
    now = datetime.now(DEFAULT_TZ)
    delta = dt - now
    total_seconds = int(delta.total_seconds())

    if total_seconds <= 0:
        return "The event is happening now or has already started! 💕", 0, True

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")

    return " • ".join(parts), days, False


def build_embed_for_guild(guild_state: dict):
    sort_events(guild_state)
    events = guild_state.get("events", [])
    embed = discord.Embed(
        title="Upcoming Event Countdowns",
        description="Live countdowns for this server’s events.",
        color=EMBED_COLOR,
    )

    if not events:
        embed.add_field(
            name="No events yet",
            value="Use `/addevent` to add one.",
            inline=False,
        )
        return embed

    any_upcoming = False

    for ev in events:
        dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
        desc, days_left, passed = compute_time_left(dt)
        date_str = dt.strftime("%B %d, %Y at %I:%M %p %Z")

        owner_id = ev.get("owner_id")
        owner_line = f"\n👤 <@{int(owner_id)}>" if owner_id else ""

        if passed:
            value = f"**{date_str}**{owner_line}\n➡️ Event has started or passed. 🎉"
        else:
            any_upcoming = True
            value = f"**{date_str}**{owner_line}\n⏱ **{desc}** remaining"

        embed.add_field(
            name=ev["name"],
            value=value,
            inline=False,
        )

    if not any_upcoming:
        embed.set_footer(text="All listed events have already started or passed.")

    return embed


async def rebuild_pinned_message(guild_id: int, channel: discord.TextChannel, guild_state: dict):
    """Unpin the old pinned countdown (if any), send a new one, and pin it."""
    sort_events(guild_state)
    old_id = guild_state.get("pinned_message_id")
    if old_id:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.unpin()
        except (discord.NotFound, discord.Forbidden):
            pass

    embed = build_embed_for_guild(guild_state)
    msg = await channel.send(embed=embed)

    try:
        await msg.pin()
    except discord.Forbidden:
        print(f"[Guild {guild_id}] Missing permission to pin messages.")
    except discord.HTTPException as e:
        print(f"[Guild {guild_id}] Failed to pin message: {e}")

    guild_state["pinned_message_id"] = msg.id
    save_state()
    return msg


async def get_or_create_pinned_message(guild_id: int, channel: discord.TextChannel):
    guild_state = get_guild_state(guild_id)
    sort_events(guild_state)
    pinned_id = guild_state.get("pinned_message_id")

    if pinned_id:
        try:
            msg = await channel.fetch_message(pinned_id)
            return msg
        except discord.NotFound:
            pass

    embed = build_embed_for_guild(guild_state)
    msg = await channel.send(embed=embed)

    try:
        await msg.pin()
    except discord.Forbidden:
        print(f"[Guild {guild_id}] Missing permission to pin messages.")
    except discord.HTTPException as e:
        print(f"[Guild {guild_id}] Failed to pin message: {e}")

    guild_state["pinned_message_id"] = msg.id
    save_state()
    return msg


# ==========================
# BACKGROUND LOOP
# ==========================

@tasks.loop(seconds=UPDATE_INTERVAL_SECONDS)
async def update_countdowns():
    guilds = state.get("guilds", {})
    for gid_str, guild_state in list(guilds.items()):
        guild_id = int(gid_str)
        sort_events(guild_state)
        channel_id = guild_state.get("event_channel_id")
        if not channel_id:
            continue

        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            continue

        # Update pinned embed
        pinned = await get_or_create_pinned_message(guild_id, channel)
        embed = build_embed_for_guild(guild_state)
        try:
            await pinned.edit(embed=embed)
        except discord.HTTPException as e:
            print(f"[Guild {guild_id}] Failed to edit pinned message: {e}")

        # Milestone checks
        for ev in guild_state.get("events", []):
            dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
            desc, days_left, passed = compute_time_left(dt)
            if passed or days_left < 0:
                continue

            milestones = ev.get("milestones", DEFAULT_MILESTONES)
            announced = ev.get("announced_milestones", [])

            if days_left in milestones and days_left not in announced:
                if days_left == 1:
                    text = f"✨ **{ev['name']}** is **tomorrow**! ✨"
                else:
                    text = (
                        f"💌 **{ev['name']}** is **{days_left} day"
                        f"{'s' if days_left != 1 else ''}** away!"
                    )

                await channel.send(text)

                announced.append(days_left)
                ev["announced_milestones"] = announced
                save_state()


# ==========================
# SLASH COMMANDS
# ==========================

def format_events_list(guild_state: dict) -> str:
    sort_events(guild_state)
    events = guild_state.get("events", [])
    if not events:
        return (
            "There are no events set for this server yet.\n"
            "Add one with `/addevent`."
        )

    lines = []
    for idx, ev in enumerate(events, start=1):
        dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
        desc, days_left, passed = compute_time_left(dt)
        status = "✅ done" if passed else "⏳ active"

        owner_id = ev.get("owner_id")
        owner_tag = f" — <@{int(owner_id)}>" if owner_id else ""
        lines.append(
            f"**{idx}. {ev['name']}** — {dt.strftime('%m/%d/%Y %H:%M')} "
            f"({desc}) [{status}]{owner_tag}"
        )
    return "\n".join(lines)


@bot.tree.command(name="seteventchannel", description="Set this channel as the event countdown channel.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def seteventchannel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    guild_state["event_channel_id"] = interaction.channel.id
    guild_state["pinned_message_id"] = None
    sort_events(guild_state)
    save_state()

    await interaction.response.send_message(
        "✅ This channel is now the event countdown channel for this server.\n"
        "Use `/addevent` to add events.",
        ephemeral=True,
    )


@bot.tree.command(name="linkserver", description="Link yourself to this server for DM control.")
@app_commands.guild_only()
async def linkserver(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    member = interaction.user
    if not isinstance(member, discord.Member):
        member = await get_member_or_fetch(guild, interaction.user.id)

    if not member or not member_is_event_manager(member, guild_state):
        await interaction.response.send_message(
            "You need to be a server event manager to link DM control.\n"
            "(Manage Server / Administrator, or a configured event admin role.)",
            ephemeral=True,
        )
        return

    user_links = get_user_links()
    user_links[str(interaction.user.id)] = guild.id
    save_state()

    await interaction.response.send_message(
        "🔗 Linked your user to this server.\n"
        "You can now DM me `/addevent` and I’ll add events to this server (as long as you still have event manager access).",
        ephemeral=True,
    )


@bot.tree.command(name="addeventadminrole", description="Allow a role to manage any ChronoBot event.")
@app_commands.describe(role="Role that can manage events")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def addeventadminrole(interaction: discord.Interaction, role: discord.Role):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    role_ids = set(guild_state.get("event_admin_role_ids", []))
    role_ids.add(role.id)
    guild_state["event_admin_role_ids"] = sorted(role_ids)
    save_state()

    await interaction.response.send_message(
        f"✅ {role.mention} can now manage any ChronoBot event in this server.",
        ephemeral=True,
    )


@bot.tree.command(name="removeeventadminrole", description="Remove a role from ChronoBot event management.")
@app_commands.describe(role="Role to remove")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def removeeventadminrole(interaction: discord.Interaction, role: discord.Role):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    role_ids = set(guild_state.get("event_admin_role_ids", []))
    role_ids.discard(role.id)
    guild_state["event_admin_role_ids"] = sorted(role_ids)
    save_state()

    await interaction.response.send_message(
        f"✅ Removed {role.mention} from ChronoBot event managers.",
        ephemeral=True,
    )


@bot.tree.command(name="listeventadminroles", description="List roles allowed to manage any ChronoBot event.")
@app_commands.guild_only()
async def listeventadminroles(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    role_ids = guild_state.get("event_admin_role_ids", [])
    if not role_ids:
        await interaction.response.send_message(
            "No event admin roles set.\n"
            "Use `/addeventadminrole` to add one (Manage Server required).",
            ephemeral=True,
        )
        return

    mentions: list[str] = []
    for rid in role_ids:
        r = guild.get_role(int(rid))
        mentions.append(r.mention if r else f"(deleted role {rid})")

    await interaction.response.send_message(
        "Event admin roles:\n" + "\n".join(f"• {m}" for m in mentions),
        ephemeral=True,
    )


@bot.tree.command(name="allowmemberaddevents", description="Toggle whether members can create their own events.")
@app_commands.describe(enabled="If true, members may add events and become the owner")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def allowmemberaddevents(interaction: discord.Interaction, enabled: bool):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    guild_state["allow_member_event_creation"] = bool(enabled)
    save_state()

    await interaction.response.send_message(
        f"✅ Member event creation is now **{'enabled' if enabled else 'disabled'}**.",
        ephemeral=True,
    )

@bot.tree.command(name="addevent", description="Add a new event to the countdown.")
@app_commands.describe(
    date="Date in MM/DD/YYYY format",
    time="Time in 24-hour HH:MM format (server timezone)",
    name="Name of the event",
)
async def addevent(interaction: discord.Interaction, date: str, time: str, name: str):
    user = interaction.user

    # ---------------------------
    # Decide which guild to target
    # ---------------------------
    if interaction.guild is not None:
        guild = interaction.guild
        guild_state = get_guild_state(guild.id)

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = await get_member_or_fetch(guild, user.id)

        if member is None:
            await interaction.response.send_message(
                "I couldn't verify your membership in this server.",
                ephemeral=True,
            )
            return

        allow_members = bool(guild_state.get("allow_member_event_creation", False))
        if not allow_members and not member_is_event_manager(member, guild_state):
            await interaction.response.send_message(
                "You don't have permission to add events here.\n"
                "Ask an admin to either:\n"
                "• enable `/allowmemberaddevents`, or\n"
                "• add your role via `/addeventadminrole`.",
                ephemeral=True,
            )
            return

        is_dm = False

    else:
        # In DMs: use linked server (permission check still enforced)
        user_links = get_user_links()
        linked_guild_id = user_links.get(str(user.id))
        if not linked_guild_id:
            await interaction.response.send_message(
                "I don't know which server to use for your DMs yet.\n"
                "In the server you want to control, run `/linkserver`, then use `/addevent` here again.",
                ephemeral=True,
            )
            return

        guild = bot.get_guild(linked_guild_id)
        if not guild:
            await interaction.response.send_message(
                "I can't find the linked server anymore. Maybe I was removed from it?\n"
                "Re-add me and run `/linkserver` again.",
                ephemeral=True,
            )
            return

        guild_state = get_guild_state(guild.id)
        member = await get_member_or_fetch(guild, user.id)
        if member is None:
            await interaction.response.send_message(
                "I can't confirm you're still in that server.",
                ephemeral=True,
            )
            return

        allow_members = bool(guild_state.get("allow_member_event_creation", False))
        if not allow_members and not member_is_event_manager(member, guild_state):
            await interaction.response.send_message(
                "You no longer have permission to add events to that server.",
                ephemeral=True,
            )
            return

        is_dm = True

    # ---------------------------
    # Make sure we have an events channel
    # ---------------------------
    if not guild_state.get("event_channel_id"):
        msg = (
            "I don't know which channel to use yet.\n"
            "Run `/seteventchannel` in the channel where you want the countdown pinned."
        )
        if is_dm:
            msg += "\n(Do this in the linked server.)"
        await interaction.response.send_message(msg, ephemeral=True)
        return

    # ---------------------------
    # Parse date/time (MM/DD/YYYY HH:MM)
    # ---------------------------
    try:
        dt = datetime.strptime(f"{date} {time}", "%m/%d/%Y %H:%M")
    except ValueError:
        await interaction.response.send_message(
            "I couldn't understand that date/time.\n"
            "Use something like: `date: 04/12/2026` `time: 09:00` (MM/DD/YYYY and 24-hour time).",
            ephemeral=True,
        )
        return

    dt = dt.replace(tzinfo=DEFAULT_TZ)

    event = {
        "name": name,
        "timestamp": int(dt.timestamp()),
        "owner_id": int(user.id),
        "milestones": DEFAULT_MILESTONES.copy(),
        "announced_milestones": [],
    }

    guild_state["events"].append(event)
    sort_events(guild_state)
    save_state()

    # Rebuild pinned message with the new full event list
    channel_id = guild_state.get("event_channel_id")
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.response.send_message(
        f"✅ Added event **{name}** on {dt.strftime('%B %d, %Y at %I:%M %p %Z')} "
        f"in server **{guild.name}**.",
        ephemeral=True,
    )
    
@bot.tree.command(name="listevents", description="List all events for this server.")
@app_commands.guild_only()
async def listevents(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    text = format_events_list(guild_state)
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(
    name="removeevent",
    description="Remove an event by its list number (from /listevents)."
)
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)"
)
@app_commands.guild_only()
async def removeevent(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None  # guaranteed by @guild_only

    guild_state = get_guild_state(guild.id)
    # Make sure events are in soonest → farthest order
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message(
            "There are no events to remove.",
            ephemeral=True,
        )
        return

    if index < 1 or index > len(events):
        await interaction.response.send_message(
            f"Index must be between 1 and {len(events)}.",
            ephemeral=True,
        )
        return

    target = events[index - 1]
    if not await can_edit_event(guild, guild_state, interaction.user.id, target):
        owner_id = target.get("owner_id")
        owner_hint = f" (<@{int(owner_id)}>)" if owner_id else ""
        await interaction.response.send_message(
            "You can't edit that event.\n"
            f"Only the event owner{owner_hint} or a server event manager can remove it.",
            ephemeral=True,
        )
        return

    # Remove the chosen event
    ev = events.pop(index - 1)
    save_state()

    # Rebuild the pinned countdown so the list is accurate
    channel_id = guild_state.get("event_channel_id")
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.response.send_message(
        f"🗑 Removed event **{ev['name']}**.",
        ephemeral=True,
    )


@bot.tree.command(name="seteventowner", description="Set or transfer the owner of an event.")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    owner="New event owner",
)
@app_commands.guild_only()
async def seteventowner(interaction: discord.Interaction, index: int, owner: discord.Member):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.response.send_message(
            "There are no events to update.",
            ephemeral=True,
        )
        return

    if index < 1 or index > len(events):
        await interaction.response.send_message(
            f"Index must be between 1 and {len(events)}.",
            ephemeral=True,
        )
        return

    target = events[index - 1]
    if not await can_edit_event(guild, guild_state, interaction.user.id, target):
        await interaction.response.send_message(
            "You can't change the owner of that event. Only the current owner or a server event manager can.",
            ephemeral=True,
        )
        return

    target["owner_id"] = int(owner.id)
    save_state()

    channel_id = guild_state.get("event_channel_id")
    channel = bot.get_channel(channel_id) if channel_id else None
    if isinstance(channel, discord.TextChannel):
        await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.response.send_message(
        f"✅ Updated owner for **{target['name']}** to {owner.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="update_countdown", description="Force-refresh the pinned countdown.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.guild_only()
async def update_countdown_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    channel_id = guild_state.get("event_channel_id")

    if not channel_id:
        await interaction.response.send_message(
            "No events channel set yet. Run `/seteventchannel` in your events channel.",
            ephemeral=True,
        )
        return

    if interaction.channel_id != channel_id:
        await interaction.response.send_message(
            "Please run this command in the configured events channel.",
            ephemeral=True,
        )
        return

    channel = interaction.channel
    assert isinstance(channel, discord.TextChannel)

    pinned = await get_or_create_pinned_message(guild.id, channel)
    embed = build_embed_for_guild(guild_state)
    await pinned.edit(embed=embed)

    await interaction.response.send_message(
        "⏱ Countdown updated.",
        ephemeral=True,
    )


@bot.tree.command(name="resendsetup", description="Resend the onboarding/setup message.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def resendsetup(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    g_state = get_guild_state(guild.id)
    g_state["welcomed"] = False
    save_state()

    await send_onboarding_for_guild(guild)
    await interaction.response.send_message(
        "📨 Setup instructions have been resent to the server owner (or a fallback channel).",
        ephemeral=True,
    )


@bot.tree.command(name="chronohelp", description="Show ChronoBot setup & command help.")
async def chronohelp(interaction: discord.Interaction):
    text = (
        "**ChronoBot – Setup & Commands**\n\n"
        "All slash command responses are ephemeral, so only you see them.\n\n"
        "1️⃣ Pick your events channel (in a server):\n"
        "   • Go to the channel you want the pinned countdown in.\n"
        "   • Run: `/seteventchannel`\n\n"
        "2️⃣ Add an event (MM/DD/YYYY):\n"
        "   • Example: `/addevent date: 04/12/2026 time: 09:00 name: Couples Retreat 💕`\n"
        "   • Format: `MM/DD/YYYY` and 24-hour `HH:MM` (server timezone).\n\n"
        "3️⃣ Manage events (in a server):\n"
        "   • `/listevents` – show all events (soonest → farthest)\n"
        "   • `/removeevent index: <number>` – remove by list number\n"
        "   • `/listeventadminroles` – show who can manage any event\n"
        "   • `/addeventadminrole role: <role>` – let a role manage all events (Manage Server)\n"
        "   • `/allowmemberaddevents enabled: true/false` – members can create their own events (Manage Server)\n"
        "   • `/update_countdown` – force-refresh the pinned countdown\n\n"
        "4️⃣ Optional: DM control:\n"
        "   • In your server, run `/linkserver` (requires event manager access).\n"
        "   • Then DM me `/addevent` with your event details.\n\n"
        "5️⃣ Onboarding:\n"
        "   • `/resendsetup` – resend the setup guide to the server owner.\n\n"
        "I’ll keep the pinned message updated and announce milestone reminders automatically. ✨"
    )
    await interaction.response.send_message(text, ephemeral=True)


# ==========================
# RUN
# ==========================

def main():
    if not TOKEN:
        raise RuntimeError(
            "No bot token found. Set the DISCORD_BOT_TOKEN environment variable "
            "or edit the TOKEN section near the top of the file."
        )
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
