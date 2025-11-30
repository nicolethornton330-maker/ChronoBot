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
      "events": [
        {
          "name": "Couples Retreat 💕",
          "timestamp": 1771000800,          # unix seconds
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
            "events": [],
            "welcomed": False,
        }
    else:
        guilds[gid].setdefault("event_channel_id", None)
        guilds[gid].setdefault("pinned_message_id", None)
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
save_state()


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

        if passed:
            value = f"**{date_str}**\n➡️ Event has started or passed. 🎉"
        else:
            any_upcoming = True
            value = f"**{date_str}**\n⏱ **{desc}** remaining"

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
        lines.append(
            f"**{idx}. {ev['name']}** — {dt.strftime('%m/%d/%Y %H:%M')} "
            f"({desc}) [{status}]"
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
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def linkserver(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    user_links = get_user_links()
    user_links[str(interaction.user.id)] = guild.id
    save_state()

    await interaction.response.send_message(
        "🔗 Linked your user to this server.\n"
        "You can now DM me `/addevent` and I’ll add events to this server (as long as you still have Manage Server).",
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
        # In a server: require Manage Server OR Administrator
        guild = interaction.guild

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = guild.get_member(user.id)

        perms = getattr(member, "guild_permissions", None)
        if not perms or not (perms.manage_guild or perms.administrator):
            await interaction.response.send_message(
                "You need the **Manage Server** or **Administrator** permission "
                "to add events in this server.",
                ephemeral=True,
            )
            return

        guild_state = get_guild_state(guild.id)
        is_dm = False

    else:
        # In DMs: use linked server (no extra member/permission check here)
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

        # At this point we trust the link: the user who linked is the one using DMs.
        guild_state = get_guild_state(guild.id)
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
@app_commands.checks.has_permissions(manage_guild=True)
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
        "   • `/update_countdown` – force-refresh the pinned countdown\n\n"
        "4️⃣ Optional: DM control:\n"
        "   • In your server, run `/linkserver` (requires Manage Server).\n"
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
