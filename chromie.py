import os
import json
from pathlib import Path
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple
import time
import discord
from discord.ext import commands, tasks
from discord import app_commands
from threading import Lock
import random

# ==========================
# CONFIG
# ==========================

VERSION = "2025-12-25-Functional-1+Autocomplete+AutoPrune"

DEFAULT_TZ = ZoneInfo("America/Chicago")
UPDATE_INTERVAL_SECONDS = 60
DEFAULT_MILESTONES = [100, 50, 30, 14, 7, 2, 1, 0]

DATA_FILE = Path(os.getenv("CHROMIE_DATA_PATH", "/var/data/chromie_state.json"))
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

EMBED_COLOR = discord.Color.from_rgb(140, 82, 255)  # ChronoBot purple

LOG_THROTTLE_SECONDS = 60 * 30  # 30 minutes
_last_log = {}  # (guild_id, code) -> last_time

_STATE_LOCK = Lock()

def log_throttled(guild_id: int, code: str, msg: str):
    key = (guild_id, code)
    now = time.time()
    last = _last_log.get(key, 0)
    if now - last >= LOG_THROTTLE_SECONDS:
        _last_log[key] = now
        print(msg)


# ==========================
# STATE HANDLING
# ==========================

"""
State structure (high level):

{
  "guilds": {
    "guild_id_str": {
      "event_channel_id": int | None,
      "pinned_message_id": int | None,
      "mention_role_id": int | None,
      "events": [
        {
          "name": str,
          "timestamp": int,
          "milestones": [int, ...],
          "announced_milestones": [int, ...],
          "repeat_every_days": int | None,
          "repeat_anchor_date": "YYYY-MM-DD" | None,
          "announced_repeat_dates": ["YYYY-MM-DD", ...],
          "silenced": bool,
          "owner_user_id": int | None
        }
      ],
      "welcomed": bool
    }
  },
  "user_links": {
    "user_id_str": guild_id_int
  }
}
"""

EVENT_START_GRACE_SECONDS = 60 * 60  # 1 hour: announce if bot wakes up within 1 hour after start

def build_event_start_blast(event_name: str) -> str:
    # Keep it short, loud, and celebratory.
    templates = [
        "🎉 IT’S HAPPENING!! 🎉 **{name}** starts **RIGHT NOW** — the countdown has officially paid off! 🥳✨",
        "🚨 TIME’S UP (in the best way) 🚨 **{name}** is **LIVE NOW**! Everybody scream internally! 🎊",
        "🔥 THE MOMENT HAS ARRIVED 🔥 **{name}** is starting **NOW**! Let’s GOOOOOO! 💜🎉",
        "✨ ZERO HAS BEEN REACHED ✨ **{name}** is happening **RIGHT NOW** — main character energy only. 🎇",
    ]
    return random.choice(templates).format(name=event_name or "The Event")

def load_state() -> dict:
    data = {}
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # Preserve the broken file so data isn't permanently lost
            try:
                ts = datetime.now(DEFAULT_TZ).strftime("%Y%m%d-%H%M%S")
                corrupt_path = DATA_FILE.with_suffix(DATA_FILE.suffix + f".corrupt.{ts}")
                DATA_FILE.rename(corrupt_path)
                print(f"[STATE] State file was invalid JSON. Renamed to: {corrupt_path.name}")
            except Exception:
                print("[STATE] State file was invalid JSON and could not be renamed.")
            data = {}

    data.setdefault("guilds", {})
    data.setdefault("user_links", {})
    return data


def sort_events(guild_state: dict):
    events = guild_state.get("events")
    if not isinstance(events, list):
        events = []
    events.sort(key=lambda ev: ev.get("timestamp", 0))
    guild_state["events"] = events


def save_state():
    with _STATE_LOCK:
        try:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

            tmp_path = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)

            os.replace(tmp_path, DATA_FILE)  # atomic on most platforms

            # Clean up if still present (paranoia)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

        except Exception as e:
            print(f"[STATE] save_state failed: {type(e).__name__}: {e}")


def get_guild_state(guild_id: int) -> dict:
    gid = str(guild_id)
    guilds = state.setdefault("guilds", {})
    if gid not in guilds:
        guilds[gid] = {
            "event_channel_id": None,
            "pinned_message_id": None,
            "mention_role_id": None,
            "events": [],
            "welcomed": False,

            # NEW (audit)
            "event_channel_set_by": None,
            "event_channel_set_at": None,
        }

    else:
        guilds[gid].setdefault("event_channel_id", None)
        guilds[gid].setdefault("pinned_message_id", None)
        guilds[gid].setdefault("mention_role_id", None)
        guilds[gid].setdefault("events", [])
        guilds[gid].setdefault("welcomed", False)
        guilds[gid].setdefault("event_channel_set_by", None)
        guilds[gid].setdefault("event_channel_set_at", None)
    return guilds[gid]


def get_user_links() -> dict:
    return state.setdefault("user_links", {})


def _today_local_date() -> date:
    return datetime.now(DEFAULT_TZ).date()


def calendar_days_left(dt: datetime) -> int:
    now = datetime.now(DEFAULT_TZ)
    return (dt.date() - now.date()).days


def compute_time_left(dt: datetime) -> Tuple[str, int, bool]:
    """Return (description, days_left_floor, event_passed)."""
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


def parse_milestones(text: str) -> Optional[List[int]]:
    """
    Parse milestone input like:
      "100, 50, 30, 14, 7, 2, 1, 0"
      "100 50 30"
      "100,50,30"
    Returns sorted unique list or None if invalid.
    """
    if not text or not text.strip():
        return None

    cleaned = text.replace(",", " ").replace(";", " ").strip()
    parts = [p for p in cleaned.split() if p.strip()]

    out: List[int] = []
    try:
        for p in parts:
            n = int(p)
            if n < 0 or n > 5000:
                return None
            out.append(n)
    except ValueError:
        return None

    out = sorted(set(out), reverse=True)
    return out


def prune_past_events(guild_state: dict, now: Optional[datetime] = None) -> int:
    """Delete events whose timestamp has passed (dt <= now). Returns # removed."""
    if now is None:
        now = datetime.now(DEFAULT_TZ)

    sort_events(guild_state)
    events = guild_state.get("events", [])
    if not isinstance(events, list) or not events:
        guild_state["events"] = [] if not isinstance(events, list) else events
        return 0

    kept: list[dict] = []
    removed = 0

    for ev in events:
        ts = ev.get("timestamp")
        if not isinstance(ts, int):
            kept.append(ev)
            continue
        try:
            dt = datetime.fromtimestamp(ts, tz=DEFAULT_TZ)
        except Exception:
            kept.append(ev)
            continue

        if dt <= now:
            age = (now - dt).total_seconds()
            start_announced = bool(ev.get("start_announced", False))

            # Remove if we already did the "started" blast,
            # OR if it's so old it's outside the grace window.
            if start_announced or age > EVENT_START_GRACE_SECONDS:
                removed += 1
            else:
                kept.append(ev)

    if removed:
        guild_state["events"] = kept
        sort_events(guild_state)

    return removed

state = load_state()

# Backfill new fields for older saved guilds/events + prune past events on boot
_boot_now = datetime.now(DEFAULT_TZ)
for _, g_state in state.get("guilds", {}).items():
    sort_events(g_state)
    g_state.setdefault("mention_role_id", None)
    g_state.setdefault("event_channel_set_by", None)
    g_state.setdefault("event_channel_set_at", None)


    for ev in g_state.get("events", []):
        ev.setdefault("milestones", DEFAULT_MILESTONES.copy())
        ev.setdefault("announced_milestones", [])
        ev.setdefault("repeat_every_days", None)
        ev.setdefault("repeat_anchor_date", None)
        ev.setdefault("announced_repeat_dates", [])
        ev.setdefault("silenced", False)
        ev.setdefault("owner_user_id", None)
        ev.setdefault("start_announced", False)


    removed = prune_past_events(g_state, now=_boot_now)
    if removed:
        # optional log
        # print(f"[STATE] Pruned {removed} past event(s) on startup.")
        pass

save_state()


# ==========================
# DISCORD SETUP
# ==========================

intents = discord.Intents.default()


class ChromieBot(commands.Bot):
    async def setup_hook(self):
        try:
            await self.tree.sync()
            print(f"Slash commands synced (setup_hook). [{VERSION}]")
        except Exception as e:
            print(f"Error syncing commands (setup_hook): {e}")

        if not update_countdowns.is_running():
            update_countdowns.start()


bot = ChromieBot(command_prefix="!", intents=intents)


# ==========================
# PERMISSION NOTIFY (OWNER DM)
# ==========================

PERM_ALERT_COOLDOWN_SECONDS = 60 * 60 * 24  # 1 day (persisted in JSON state)

RECOMMENDED_CHANNEL_PERMS = (
    "view_channel",
    "send_messages",
    "embed_links",
    "read_message_history",
    "manage_messages",  # needed for pin/unpin + editing
)

PERM_LABELS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "read_message_history": "Read Message History",
    "manage_messages": "Manage Messages (pin/unpin)",
}


def _bot_member_cached(guild: discord.Guild) -> Optional[discord.Member]:
    if guild.me is not None:
        return guild.me
    if bot.user is None:
        return None
    return guild.get_member(bot.user.id)


def missing_channel_perms(channel: discord.abc.GuildChannel, guild: discord.Guild) -> list[str]:
    me = _bot_member_cached(guild)
    if me is None:
        return list(RECOMMENDED_CHANNEL_PERMS)

    perms = channel.permissions_for(me)
    return [p for p in RECOMMENDED_CHANNEL_PERMS if not getattr(perms, p, False)]


def _perm_alert_key(guild_id: int, channel_id: int, code: str) -> str:
    return f"{guild_id}:{channel_id}:{code}"


def _get_perm_alerts_bucket(guild_state: dict) -> dict:
    # Stored in chromie_state.json so Render restarts don’t reset the cooldown
    bucket = guild_state.get("perm_alerts")
    if not isinstance(bucket, dict):
        bucket = {}
        guild_state["perm_alerts"] = bucket
    return bucket


def _should_send_perm_alert(guild_state: dict, key: str) -> bool:
    bucket = _get_perm_alerts_bucket(guild_state)
    now = int(time.time())
    last = int(bucket.get(key, 0) or 0)
    return (now - last) >= PERM_ALERT_COOLDOWN_SECONDS


def _mark_perm_alert_sent(guild_state: dict, key: str):
    bucket = _get_perm_alerts_bucket(guild_state)
    bucket[key] = int(time.time())


def build_perm_howto(channel: discord.abc.GuildChannel, missing: list[str]) -> str:
    pretty = "\n".join(f"• {PERM_LABELS.get(p, p)}" for p in missing)
    ch_name = getattr(channel, "name", "this channel")

    return (
        f"**Missing permissions in #{ch_name}:**\n"
        f"{pretty}\n\n"
        "**Fix (channel-specific):**\n"
        f"1) Right-click **#{ch_name}** → **Edit Channel**\n"
        "2) Go to **Permissions**\n"
        "3) **Add Members or Roles** → select **ChronoBot/Chromie** (or its role)\n"
        "4) Set these to **Allow (✅)**:\n"
        "   • View Channel\n"
        "   • Send Messages\n"
        "   • Embed Links\n"
        "   • Read Message History\n"
        "   • Manage Messages\n"
        "5) Remove any **red ❌ denies** for the bot/role (deny overrides allow)\n\n"
        "✅ Then run `/healthcheck` to confirm everything is fixed."
    )


async def notify_owner_missing_perms(
    guild: discord.Guild,
    channel: Optional[discord.abc.GuildChannel],
    *,
    missing: list[str],
    action: str,
):
    """DM server owner once per day per (guild, channel, missing-perms-set) about a permission issue."""
    if not missing:
        return

    guild_state = get_guild_state(guild.id)
    channel_id = getattr(channel, "id", 0) or 0

    # This makes “once per day per missing permission” behave the way you want:
    # same missing set + same channel + same action => 1 alert/day
    code = f"{action}|" + ",".join(sorted(missing))
    key = _perm_alert_key(guild.id, channel_id, code)

    if not _should_send_perm_alert(guild_state, key):
        return

    chan_name = f"#{getattr(channel, 'name', 'unknown')}" if channel else "(unknown channel)"
    header = (
        "⚠️ **ChronoBot permission issue**\n\n"
        f"I tried to **{action}** in **{chan_name}** on **{guild.name}**, but I’m missing permissions.\n\n"
    )

    howto = build_perm_howto(channel, missing) if channel else (
        "Please ensure the bot can View Channel, Send Messages, Embed Links, Read Message History, "
        "and Manage Messages in the channel you set for countdowns.\n\n"
        "✅ Then run `/healthcheck` to confirm everything is fixed."
    )

    footer = (
        "\n\n📅 I’ll only send one reminder per day for this specific issue.\n"
        "Next step: run `/healthcheck` (Manage Server) for diagnostics."
    )

    text = header + howto + footer

    # Try DM owner
    owner = guild.owner
    if owner is None:
        try:
            owner = await bot.fetch_user(guild.owner_id)
        except Exception:
            owner = None

    sent = False
    if owner:
        try:
            await owner.send(text)
            sent = True
        except (discord.Forbidden, discord.HTTPException):
            sent = False

    # Fallback: system channel or first sendable channel
    if not sent:
        fallback = guild.system_channel
        if fallback is None:
            me = _bot_member_cached(guild)
            for ch in guild.text_channels:
                if me and ch.permissions_for(me).send_messages:
                    fallback = ch
                    break

        if fallback:
            try:
                await fallback.send(text, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass

    # Mark (even if delivery failed) to avoid spam loops; will try again tomorrow
    _mark_perm_alert_sent(guild_state, key)
    save_state()

async def notify_event_channel_changed(
    guild: discord.Guild,
    *,
    actor: discord.abc.User,
    old_channel_id: Optional[int],
    new_channel: discord.TextChannel,
):
    """Notify owner + optionally post a lightweight audit message when event channel changes."""
    when = datetime.now(DEFAULT_TZ).strftime("%B %d, %Y at %I:%M %p %Z")

    old_ch_mention = "(not set)"
    if old_channel_id:
        old_ch = await get_text_channel(int(old_channel_id))
        if old_ch is not None:
            old_ch_mention = old_ch.mention
        else:
            old_ch_mention = f"(unknown channel id {old_channel_id})"

    msg = (
        "🔧 **ChronoBot configuration updated**\n"
        f"• Event channel: {old_ch_mention} → {new_channel.mention}\n"
        f"• Changed by: {getattr(actor, 'name', 'unknown')} (ID: {actor.id})\n"
        f"• When: {when}"
    )

    # ---- DM server owner (primary) ----
    owner = guild.owner
    if owner is None:
        try:
            owner = await bot.fetch_user(guild.owner_id)
        except Exception:
            owner = None

    if owner:
        try:
            await owner.send(msg)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ---- Optional: post audit note in the NEW channel (nice for transparency) ----
    try:
        bot_member = await get_bot_member(guild)
        if bot_member and new_channel.permissions_for(bot_member).send_messages:
            await new_channel.send(msg, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        pass

    # ---- Optional: post a breadcrumb in the OLD channel (helps people discover the move) ----
    if old_channel_id and int(old_channel_id) != int(new_channel.id):
        try:
            old_ch = await get_text_channel(int(old_channel_id))
            if old_ch:
                bot_member = await get_bot_member(guild)
                if bot_member and old_ch.permissions_for(bot_member).send_messages:
                    await old_ch.send(
                        f"🔁 ChronoBot event channel was moved to {new_channel.mention} on {when}.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
        except Exception:
            pass


async def get_bot_member(guild: discord.Guild) -> Optional[discord.Member]:
    if not bot.user:
        return None
    m = guild.get_member(bot.user.id)
    if m:
        return m
    try:
        return await guild.fetch_member(bot.user.id)
    except Exception:
        return None


async def send_onboarding_for_guild(guild: discord.Guild):
    guild_state = get_guild_state(guild.id)

    if guild_state.get("welcomed"):
        return

    contact_user = guild.owner or (await bot.fetch_user(guild.owner_id))
    mention = contact_user.mention if contact_user else ""
    milestone_str = ", ".join(str(x) for x in DEFAULT_MILESTONES)

    setup_message = (
        f"Hey {mention}! Thanks for inviting **ChronoBot** to **{guild.name}** 🕒✨\n\n"
        "I’m **Chromie** — your server’s confident little timekeeper. I pin a clean countdown list and I nudge people at the right moments. "
        "It’s like a calendar… but with better vibes.\n\n"
        f"⏳ **Default milestones:** {milestone_str} days before the event (including **0** for day-of).\n\n"
        "**⚡ Quick start (two commands, instant order):**\n"
        "1) In your events channel: `/seteventchannel`\n"
        "2) Add your first event: `/addevent date: 04/12/2026 time: 09:00 name: Game Night 🎲`\n\n"
        "**🧰 Handy commands:**\n"
        "• `/editevent` – tweak name/date/time without re-adding\n"
        "• `/remindall` – ping the channel about the next (or chosen) event\n"
        "• `/dupeevent` – clone an event (perfect for yearly stuff)\n"
        "• `/seteventowner` – assign an owner and I’ll DM them at reminders\n"
        "• `/setrepeat index: <number> every_days: <days>` – repeating reminders every X days\n"
        "   - Daily example: `/setrepeat index: 1 every_days: 1`\n"
        "   - Weekly example: `/setrepeat index: 1 every_days: 7`\n"
        "• `/clearrepeat index: <number>` – turn repeating reminders off\n\n"
        "Need the full menu? Type `/chronohelp` and I’ll hand you the whole spellbook.\n\n"
        "Alright — I’ll be over here, politely bullying time into behaving. 💜"
    )

    sent = False
    if contact_user:
        try:
            await contact_user.send(setup_message)
            sent = True
        except discord.Forbidden:
            sent = False

    if not sent:
        fallback_channel = guild.system_channel

        if fallback_channel is None:
            bot_m = await get_bot_member(guild)
            for ch in guild.text_channels:
                target = bot_m if bot_m is not None else guild.default_role
                perms = ch.permissions_for(target)
                if perms.view_channel and perms.send_messages:
                    fallback_channel = ch
                    break

        if fallback_channel is not None:
            try:
                await fallback_channel.send(setup_message, allowed_mentions=discord.AllowedMentions.none())
                sent = True
            except discord.Forbidden:
                sent = False

    guild_state["welcomed"] = True
    save_state()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id}) [{VERSION}]")


@bot.event
async def on_guild_join(guild: discord.Guild):
    g_state = get_guild_state(guild.id)
    sort_events(g_state)
    save_state()
    await send_onboarding_for_guild(guild)


# ==========================
# EMBED HELPERS
# ==========================
MAX_EMBED_EVENTS = 25  # Discord embed field limit

def build_embed_for_guild(guild_state: dict) -> discord.Embed:
    sort_events(guild_state)
    events = guild_state.get("events", [])

    embed = discord.Embed(
        title="Upcoming Event Countdowns",
        description="Live countdowns for this server’s events.",
        color=EMBED_COLOR,
    )

    if not events:
        embed.add_field(name="No events yet", value="Use `/addevent` to add one.", inline=False)
        return embed

    shown = 0
    any_upcoming = False

    for ev in events:
        if shown >= MAX_EMBED_EVENTS:
            break

        ts = ev.get("timestamp")
        if not isinstance(ts, int):
            continue

        dt = datetime.fromtimestamp(ts, tz=DEFAULT_TZ)
        desc, _, passed = compute_time_left(dt)
        date_str = dt.strftime("%B %d, %Y at %I:%M %p %Z")

        silenced = ev.get("silenced", False)
        silenced_note = " 🔕 (silenced)" if silenced and not passed else ""

        if passed:
            value = f"**{date_str}**\n➡️ Event has started or passed. 🎉"
        else:
            any_upcoming = True
            value = f"**{date_str}**\n⏱ **{desc}** remaining{silenced_note}"

        embed.add_field(name=ev.get("name", "Event")[:256], value=value[:1024], inline=False)
        shown += 1

    if len(events) > shown:
        embed.set_footer(text=f"Showing {shown} of {len(events)} events. Use /listevents to view all.")

    if not any_upcoming:
        embed.set_footer(text="All listed events have already started or passed.")

    return embed


async def rebuild_pinned_message(guild_id: int, channel: discord.TextChannel, guild_state: dict):
    sort_events(guild_state)
    old_id = guild_state.get("pinned_message_id")
    if old_id:
        try:
            old_msg = await channel.fetch_message(int(old_id))
            await old_msg.unpin()
        except (discord.NotFound, discord.Forbidden):
            pass

    embed = build_embed_for_guild(guild_state)

    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        missing = missing_channel_perms(channel, channel.guild)
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=missing,
            action="send the countdown message",
        )
        return None
    except discord.HTTPException:
        return None

    try:
        await msg.pin()
    except discord.Forbidden:
        missing = missing_channel_perms(channel, channel.guild)
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=missing,
            action="pin the countdown message",
        )
    except discord.HTTPException as e:
        print(f"[Guild {guild_id}] Failed to pin message: {e}")

    guild_state["pinned_message_id"] = msg.id
    save_state()
    return msg


async def get_or_create_pinned_message(
    guild_id: int,
    channel: discord.TextChannel,
    *,
    allow_create: bool = False,
):
    guild_state = get_guild_state(guild_id)
    sort_events(guild_state)
    pinned_id = guild_state.get("pinned_message_id")

    bot_member = await get_bot_member(channel.guild)
    if bot_member is None:
        return None

    perms = channel.permissions_for(bot_member)
    if not perms.view_channel or not perms.send_messages:
        return None

    if pinned_id:
        if not perms.read_message_history:
            return None

        try:
            return await channel.fetch_message(int(pinned_id))
        except discord.NotFound:
            guild_state["pinned_message_id"] = None
            save_state()
            pinned_id = None
        except discord.Forbidden:
            return None
        except discord.HTTPException:
            return None

    if not allow_create:
        return None

    embed = build_embed_for_guild(guild_state)
    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        missing = missing_channel_perms(channel, channel.guild)
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=missing,
            action="send the countdown message",
        )
        return None
    except discord.HTTPException:
        return None

    if perms.manage_messages:
        try:
            await msg.pin()
        except discord.Forbidden:
            missing = missing_channel_perms(channel, channel.guild)
            await notify_owner_missing_perms(
                channel.guild,
                channel,
                missing=missing,
                action="pin the countdown message",
            )
        except Exception:
            pass

    guild_state["pinned_message_id"] = msg.id
    save_state()
    return msg


async def get_text_channel(channel_id: int) -> Optional[discord.TextChannel]:
    ch = bot.get_channel(channel_id)
    if isinstance(ch, discord.TextChannel):
        return ch
    try:
        ch = await bot.fetch_channel(channel_id)
        return ch if isinstance(ch, discord.TextChannel) else None
    except Exception:
        return None


async def dm_owner_if_set(guild: discord.Guild, ev: dict, message: str):
    owner_id = ev.get("owner_user_id")
    if not owner_id:
        return
    try:
        user = guild.get_member(owner_id) or await bot.fetch_user(owner_id)
        if user:
            await user.send(message)
    except discord.Forbidden:
        pass
    except Exception:
        pass


def get_event_by_index(guild_state: dict, index: int) -> Optional[dict]:
    sort_events(guild_state)
    events = guild_state.get("events", [])
    if index < 1 or index > len(events):
        return None
    return events[index - 1]


def build_milestone_mention(channel: discord.TextChannel, guild_state: dict) -> Tuple[str, discord.AllowedMentions]:
    role_id = guild_state.get("mention_role_id")
    if role_id:
        role = channel.guild.get_role(int(role_id))
        if role:
            return f"{role.mention} ", discord.AllowedMentions(roles=True)
    return "", discord.AllowedMentions.none()

def build_everyone_mention() -> Tuple[str, discord.AllowedMentions]:
    return "@everyone ", discord.AllowedMentions(everyone=True)


# ==========================
# AUTOCOMPLETE HELPERS
# ==========================

async def event_index_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[int]]:
    """Autocomplete for any command `index` param: shows '1. Name — mm/dd/yyyy hh:mm'."""
    guild = interaction.guild
    if guild is None:
        return []

    g = get_guild_state(guild.id)
    sort_events(g)

    now = datetime.now(DEFAULT_TZ)
    cur = (current or "").strip().lower()

    choices: List[app_commands.Choice[int]] = []
    for idx, ev in enumerate(g.get("events", []), start=1):
        ts = ev.get("timestamp")
        if not isinstance(ts, int):
            continue
        try:
            dt = datetime.fromtimestamp(ts, tz=DEFAULT_TZ)
        except Exception:
            continue

        if dt <= now:
            continue  # should already be pruned, but safe

        label = f"{idx}. {ev.get('name', 'Event')} — {dt.strftime('%m/%d/%Y %H:%M')}"
        label_l = label.lower()
        name_l = (ev.get("name") or "").lower()

        if cur:
            if cur.isdigit():
                if not str(idx).startswith(cur) and cur not in name_l:
                    continue
            else:
                if cur not in name_l and cur not in label_l:
                    continue

        choices.append(app_commands.Choice(name=label[:100], value=idx))
        if len(choices) >= 25:
            break

    return choices


# ==========================
# BACKGROUND LOOP
# ==========================

@tasks.loop(seconds=UPDATE_INTERVAL_SECONDS)
async def update_countdowns():
    guilds = state.get("guilds", {})
    for gid_str, guild_state in list(guilds.items()):
        try:
            guild_id = int(gid_str)
            sort_events(guild_state)

            channel_id = guild_state.get("event_channel_id")
            if not channel_id:
                continue

            channel = await get_text_channel(channel_id)
            if channel is None:
                continue

            bot_member = await get_bot_member(channel.guild)
            if bot_member is None:
                continue

            state_changed = False

            # ---- EVENT CHECKS (start blast + milestones + repeats) ----
            today = _today_local_date()
            now = datetime.now(DEFAULT_TZ)

            for ev in list(guild_state.get("events", [])):
                if ev.get("silenced", False):
                    continue

                ts = ev.get("timestamp")
                if not isinstance(ts, int):
                    continue

                try:
                    dt = datetime.fromtimestamp(ts, tz=DEFAULT_TZ)
                except Exception:
                    continue

                # ---- EVENT START BLAST (time-of-event) ----
                if dt <= now:
                    if not bool(ev.get("start_announced", False)):
                        age = (now - dt).total_seconds()
                        if age <= EVENT_START_GRACE_SECONDS:
                            mention_prefix = ""
                            allowed = discord.AllowedMentions.none()

                            perms = channel.permissions_for(bot_member)
                            if perms.mention_everyone:
                                mention_prefix, allowed = build_everyone_mention()
                            else:
                                mention_prefix, allowed = build_milestone_mention(channel, guild_state)

                            text = mention_prefix + build_event_start_blast(ev.get("name", "Event"))

                            try:
                                await channel.send(text, allowed_mentions=allowed)
                                ev["start_announced"] = True
                                save_state()
                                state_changed = True
                            except discord.Forbidden:
                                missing = missing_channel_perms(channel, channel.guild)
                                await notify_owner_missing_perms(
                                    channel.guild,
                                    channel,
                                    missing=missing,
                                    action="send the event start announcement",
                                )
                            except discord.HTTPException as e:
                                print(f"[Guild {guild_id}] Failed to send start blast: {e}")

                    continue  # don’t do milestones/repeats for started/past events

                # ---- Milestones + repeating reminders (your existing logic) ----
                desc, _, passed = compute_time_left(dt)
                if passed:
                    continue

                days_left = calendar_days_left(dt)
                if days_left < 0:
                    continue

                milestone_sent_today = False

                milestones = ev.get("milestones", DEFAULT_MILESTONES)
                announced = ev.get("announced_milestones", [])
                if not isinstance(announced, list):
                    announced = []
                    ev["announced_milestones"] = announced

                if days_left in milestones and days_left not in announced:
                    mention_prefix, allowed_mentions = build_milestone_mention(channel, guild_state)

                    if days_left == 0:
                        text = f"{mention_prefix}🎉 **{ev.get('name', 'Event')}** is **today**! 🎉"
                    elif days_left == 1:
                        text = f"{mention_prefix}✨ **{ev.get('name', 'Event')}** is **tomorrow**! ✨"
                    else:
                        text = (
                            f"{mention_prefix}💌 **{ev.get('name', 'Event')}** is **{days_left} day"
                            f"{'s' if days_left != 1 else ''}** away!"
                        )

                    try:
                        await channel.send(text, allowed_mentions=allowed_mentions)
                        announced.append(days_left)
                        ev["announced_milestones"] = announced
                        save_state()
                        state_changed = True
                        milestone_sent_today = True
                    except discord.Forbidden:
                        missing = missing_channel_perms(channel, channel.guild)
                        await notify_owner_missing_perms(
                            channel.guild,
                            channel,
                            missing=missing,
                            action="send milestone reminders",
                        )
                        continue

                    try:
                        await dm_owner_if_set(
                            channel.guild,
                            ev,
                            f"⏰ Milestone: **{ev.get('name', 'Event')}** is in **{days_left} day{'s' if days_left != 1 else ''}** "
                            f"(on {dt.strftime('%B %d, %Y at %I:%M %p %Z')})."
                        )
                    except Exception:
                        pass

                repeat_every = ev.get("repeat_every_days")
                if isinstance(repeat_every, int) and repeat_every > 0:
                    anchor_str = ev.get("repeat_anchor_date") or today.isoformat()
                    try:
                        anchor = date.fromisoformat(anchor_str)
                    except ValueError:
                        anchor = today
                        ev["repeat_anchor_date"] = anchor.isoformat()

                    days_since_anchor = (today - anchor).days
                    if days_since_anchor > 0 and (days_since_anchor % repeat_every == 0):
                        sent_dates = ev.get("announced_repeat_dates", [])
                        if not isinstance(sent_dates, list):
                            sent_dates = []
                            ev["announced_repeat_dates"] = sent_dates

                        if today.isoformat() not in sent_dates and not milestone_sent_today:
                            try:
                                date_str = dt.strftime("%B %d, %Y")
                                await channel.send(
                                    f"🔁 Reminder: **{ev.get('name', 'Event')}** is in **{desc}** (on **{date_str}**).",
                                    allowed_mentions=discord.AllowedMentions.none(),
                                )
                                sent_dates.append(today.isoformat())
                                ev["announced_repeat_dates"] = sent_dates[-180:]
                                save_state()
                                state_changed = True
                            except discord.Forbidden:
                                missing = missing_channel_perms(channel, channel.guild)
                                await notify_owner_missing_perms(
                                    channel.guild,
                                    channel,
                                    missing=missing,
                                    action="send repeating reminders",
                                )
                            except discord.HTTPException as e:
                                print(f"[Guild {guild_id}] Failed to send repeat reminder: {e}")

                            try:
                                await dm_owner_if_set(
                                    channel.guild,
                                    ev,
                                    f"🔁 Repeat reminder: **{ev.get('name', 'Event')}** is in **{desc}** "
                                    f"(on {dt.strftime('%B %d, %Y at %I:%M %p %Z')})."
                                )
                            except Exception:
                                pass

            # ---- Prune after processing (so start blast can happen) ----
            removed = prune_past_events(guild_state, now=datetime.now(DEFAULT_TZ))
            if removed:
                save_state()
                state_changed = True

            # ---- Update pinned embed once at end (reflects changes) ----
            pinned = await get_or_create_pinned_message(guild_id, channel, allow_create=True)
            if pinned is not None:
                try:
                    await pinned.edit(embed=build_embed_for_guild(guild_state))
                except discord.Forbidden:
                    missing = missing_channel_perms(channel, channel.guild)
                    await notify_owner_missing_perms(
                        channel.guild,
                        channel,
                        missing=missing,
                        action="edit/update the pinned countdown message",
                    )
                except discord.HTTPException as e:
                    print(f"[Guild {guild_id}] Failed to edit pinned message: {e}")

        except Exception as e:
            print(f"[Guild {gid_str}] update_countdowns crashed for this guild: {type(e).__name__}: {e}")
            continue

@update_countdowns.before_loop
async def before_update_countdowns():
    await bot.wait_until_ready()


# ==========================
# SLASH COMMANDS
# ==========================

async def _safe_ephemeral(interaction: discord.Interaction, content: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except Exception:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await _safe_ephemeral(interaction, "You need **Manage Server** to use that command.")
        return
    if isinstance(error, app_commands.CheckFailure):
        await _safe_ephemeral(interaction, "You don’t have permission to use that command.")
        return

    # Anything else: log for you
    print(f"[APP_COMMAND_ERROR] {type(error).__name__}: {error}")

def format_events_list(guild_state: dict) -> str:
    sort_events(guild_state)
    events = guild_state.get("events", [])
    if not events:
        return "There are no events set for this server yet.\nAdd one with `/addevent`."

    lines = []
    for idx, ev in enumerate(events, start=1):
        ts = ev.get("timestamp")
        if not isinstance(ts, int):
            continue
        dt = datetime.fromtimestamp(ts, tz=DEFAULT_TZ)
        desc, _, passed = compute_time_left(dt)
        status = "✅ done" if passed else "⏳ active"

        repeat_every = ev.get("repeat_every_days")
        repeat_note = ""
        if isinstance(repeat_every, int) and repeat_every > 0:
            repeat_note = f" 🔁 every {repeat_every} day{'s' if repeat_every != 1 else ''}"

        silenced = ev.get("silenced", False)
        silenced_note = " 🔕 silenced" if silenced and not passed else ""

        lines.append(
            f"**{idx}. {ev.get('name', 'Event')}** — {dt.strftime('%m/%d/%Y %H:%M')} "
            f"({desc}) [{status}]{repeat_note}{silenced_note}"
        )
    return "\n".join(lines)

@bot.tree.command(name="seteventchannel", description="Set this channel as the event countdown channel.")
@app_commands.default_permissions(manage_guild=True)  # ✅ hides command from non-manage-server users
@app_commands.checks.has_permissions(manage_guild=True)  # ✅ runtime enforcement
@app_commands.guild_only()
async def seteventchannel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)

    # ✅ Guard: only allow normal text channels (not threads, not forums, etc.)
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.edit_original_response(
            content="Please run `/seteventchannel` in a **regular text channel** (not a thread/forum)."
        )
        return

    # ✅ Defense-in-depth: verify perms via resolved Member object
    member = interaction.user
    if not isinstance(member, discord.Member):
        member = guild.get_member(interaction.user.id) or member  # best effort

    perms = getattr(member, "guild_permissions", None)
    if not perms or not (perms.manage_guild or perms.administrator):
        await interaction.edit_original_response(
            content="You need **Manage Server** (or **Administrator**) to change the event channel."
        )
        return

    guild_state = get_guild_state(guild.id)

    old_channel_id = guild_state.get("event_channel_id")
    new_channel = interaction.channel

    # If no-op, don’t spam notifications
    if old_channel_id and int(old_channel_id) == int(new_channel.id):
        await interaction.edit_original_response(
            content="✅ This channel is already the event countdown channel."
        )
        return

    guild_state["event_channel_id"] = new_channel.id
    guild_state["pinned_message_id"] = None

    # audit fields (optional)
    guild_state["event_channel_set_by"] = int(interaction.user.id)
    guild_state["event_channel_set_at"] = int(time.time())

    sort_events(guild_state)
    save_state()

    # Permissions check + owner DM (you already do this)
    missing: list[str] = []
    if hasattr(new_channel, "permissions_for"):
        missing = missing_channel_perms(new_channel, guild)
        if missing:
            await notify_owner_missing_perms(
                guild,
                new_channel,
                missing=missing,
                action="set up the countdown (send + pin + update)",
            )

    # 🔔 NEW: Notify owner + optionally post audit note(s)
    await notify_event_channel_changed(
        guild,
        actor=interaction.user,
        old_channel_id=old_channel_id if isinstance(old_channel_id, int) else None,
        new_channel=new_channel,
    )

    extra = ""
    if missing:
        extra = (
            "\n\n⚠️ I’m missing some permissions in this channel, so the countdown may not work yet. "
            "I’ve messaged the server owner with a quick fix guide."
        )

    await interaction.edit_original_response(
        content="✅ This channel is now the event countdown channel for this server.\nUse `/addevent` to add events." + extra
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
        "🔗 Linked your user to this server.\nYou can now DM me `/addevent` and I’ll add events to this server (Manage Server required).",
        ephemeral=True,
    )


@bot.tree.command(name="addevent", description="Add a new event to the countdown.")
@app_commands.describe(
    date="Date in MM/DD/YYYY format",
    time="Time in 24-hour HH:MM format (America/Chicago)",
    name="Name of the event",
)
async def addevent(interaction: discord.Interaction, date: str, time: str, name: str):
    await interaction.response.defer(ephemeral=True)    
    user = interaction.user

    if interaction.guild is not None:
        guild = interaction.guild

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = guild.get_member(user.id)

        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except Exception:
                member = None

        perms = getattr(member, "guild_permissions", None)
        if not perms or not (perms.manage_guild or perms.administrator):
            await interaction.edit_original_response(content=
                "You need the **Manage Server** or **Administrator** permission to add events in this server."
            )
            return

        guild_state = get_guild_state(guild.id)
        is_dm = False

    else:
        user_links = get_user_links()
        linked_guild_id = user_links.get(str(user.id))
        if not linked_guild_id:
            await interaction.edit_original_response(content=
                "I don't know which server to use for your DMs yet.\nIn the server you want to control, run `/linkserver`, then DM me `/addevent` again."
            )
            return

        guild = bot.get_guild(linked_guild_id)
        if not guild:
            await interaction.edit_original_response(content=
                "I can't find the linked server anymore. Maybe I was removed from it?\nRe-add me and run `/linkserver` again."
            )
            return

        member = guild.get_member(user.id)
        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except Exception:
                member = None

        perms = getattr(member, "guild_permissions", None)
        if not perms or not (perms.manage_guild or perms.administrator):
            await interaction.edit_original_response(content=
                "You no longer have **Manage Server** (or **Administrator**) in the linked server, so I can’t add events via DM."
            )
            return

        guild_state = get_guild_state(guild.id)
        is_dm = True

    if not guild_state.get("event_channel_id"):
        msg = "I don't know which channel to use yet.\nRun `/seteventchannel` in the channel where you want the countdown pinned."
        if is_dm:
            msg += "\n(Do this in the linked server.)"
        await interaction.edit_original_response(content=msg)
        return

    try:
        dt = datetime.strptime(f"{date} {time}", "%m/%d/%Y %H:%M")
    except ValueError:
        await interaction.edit_original_response(content=
            "I couldn't understand that date/time.\nUse: `date: 04/12/2026` `time: 09:00` (MM/DD/YYYY + 24-hour HH:MM)."
        )
        return

    dt = dt.replace(tzinfo=DEFAULT_TZ)

    if dt <= datetime.now(DEFAULT_TZ):
        await interaction.edit_original_response(content=
            "That date/time is in the past. Please choose a future time."
        )
        return

    event = {
        "name": name,
        "timestamp": int(dt.timestamp()),
        "milestones": DEFAULT_MILESTONES.copy(),
        "announced_milestones": [],
        "repeat_every_days": None,
        "repeat_anchor_date": None,
        "announced_repeat_dates": [],
        "silenced": False,
        "owner_user_id": None,
        "start_announced": False,
    }

    guild_state["events"].append(event)
    sort_events(guild_state)
    save_state()

    channel_id = guild_state.get("event_channel_id")
    if channel_id:
        channel = await get_text_channel(channel_id)
        if channel is not None:
            await rebuild_pinned_message(guild.id, channel, guild_state)

    await interaction.edit_original_response(content=
        f"✅ Added event **{name}** on {dt.strftime('%B %d, %Y at %I:%M %p %Z')} in server **{guild.name}**."
    )


@bot.tree.command(name="listevents", description="List all events for this server.")
@app_commands.guild_only()
async def listevents(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    guild_state = get_guild_state(guild.id)
    await interaction.response.send_message(format_events_list(guild_state), ephemeral=True)


@bot.tree.command(name="nextevent", description="Show the next upcoming event.")
@app_commands.guild_only()
async def nextevent(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    sort_events(g)

    now = datetime.now(DEFAULT_TZ)
    next_ev = None
    for ev in g.get("events", []):
        dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
        if dt > now:
            next_ev = (ev, dt)
            break

    if not next_ev:
        await interaction.response.send_message("No upcoming events found.", ephemeral=True)
        return

    ev, dt = next_ev
    desc, _, _ = compute_time_left(dt)
    await interaction.response.send_message(
        f"⏭️ Next event: **{ev['name']}**\n"
        f"🗓️ {dt.strftime('%B %d, %Y at %I:%M %p %Z')}\n"
        f"⏱️ {desc} remaining",
        ephemeral=True,
    )


@bot.tree.command(name="eventinfo", description="Show details for one event.")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.guild_only()
async def eventinfo(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)
    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents` to see event numbers.", ephemeral=True)
        return

    dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
    desc, _, passed = compute_time_left(dt)
    miles = ", ".join(str(x) for x in ev.get("milestones", DEFAULT_MILESTONES))
    repeat_every = ev.get("repeat_every_days")
    repeat_note = "off"
    if isinstance(repeat_every, int) and repeat_every > 0:
        repeat_note = f"every {repeat_every} day(s) (anchor: {ev.get('repeat_anchor_date')})"

    silenced = ev.get("silenced", False)
    owner_id = ev.get("owner_user_id")
    owner_note = f"<@{owner_id}>" if owner_id else "none"

    await interaction.response.send_message(
        f"**Event #{index}: {ev['name']}**\n"
        f"🗓️ {dt.strftime('%B %d, %Y at %I:%M %p %Z')}\n"
        f"⏱️ {desc} remaining\n"
        f"🔔 Milestones: {miles}\n"
        f"🔁 Repeat: {repeat_note}\n"
        f"🔕 Silenced: {'yes' if silenced and not passed else 'no'}\n"
        f"👤 Owner (DM): {owner_note}",
        ephemeral=True,
    )


@bot.tree.command(name="removeevent", description="Remove an event by its list number (from /listevents).")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def removeevent(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None
    
    await interaction.response.defer(ephemeral=True)
    guild_state = get_guild_state(guild.id)
    sort_events(guild_state)
    events = guild_state.get("events", [])

    if not events:
        await interaction.edit_original_response(content="There are no events to remove.")
        return

    if index < 1 or index > len(events):
        await interaction.edit_original_response(content=f"Index must be between 1 and {len(events)}.")
        return

    ev = events.pop(index - 1)
    save_state()

    channel_id = guild_state.get("event_channel_id")
    if channel_id:
        ch = await get_text_channel(channel_id)
        if ch:
            await rebuild_pinned_message(guild.id, ch, guild_state)

    await interaction.edit_original_response(content=f"🗑 Removed event **{ev['name']}**.")


@bot.tree.command(name="editevent", description="Edit an event's name/date/time.")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    name="New name (optional)",
    date="New date MM/DD/YYYY (optional)",
    time="New time 24-hour HH:MM (optional)",
)
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def editevent(interaction: discord.Interaction, index: int, name: Optional[str] = None, date: Optional[str] = None, time: Optional[str] = None):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)
    
    g = get_guild_state(guild.id)
    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.edit_original_response(content="Invalid index. Use `/listevents`.")
        return

    if name and name.strip():
        ev["name"] = name.strip()

    if date or time:
        current_dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
        new_date = current_dt.strftime("%m/%d/%Y")
        new_time = current_dt.strftime("%H:%M")

        if date and date.strip():
            new_date = date.strip()
        if time and time.strip():
            new_time = time.strip()

        try:
            dt = datetime.strptime(f"{new_date} {new_time}", "%m/%d/%Y %H:%M").replace(tzinfo=DEFAULT_TZ)
        except ValueError:
            await interaction.edit_original_response(content=
                "I couldn't understand that date/time.\nUse MM/DD/YYYY + 24-hour HH:MM."
            )
            return

        if dt <= datetime.now(DEFAULT_TZ):
            await interaction.edit_original_response(content="That date/time is in the past. Please choose a future time.")
            return

        ev["timestamp"] = int(dt.timestamp())
        ev["announced_milestones"] = []
        ev["announced_repeat_dates"] = []

    sort_events(g)
    save_state()

    ch_id = g.get("event_channel_id")
    if ch_id:
        ch = await get_text_channel(ch_id)
        if ch:
            await rebuild_pinned_message(guild.id, ch, g)

    dt_final = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
    await interaction.edit_original_response(content=
        f"✅ Updated event #{index}: **{ev['name']}**\n"
        f"🗓️ {dt_final.strftime('%B %d, %Y at %I:%M %p %Z')}"
    )


@bot.tree.command(name="dupeevent", description="Duplicate an event (optional time/name).")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    date="New date MM/DD/YYYY",
    time="New time 24-hour HH:MM (optional; defaults to original time)",
    name="New name (optional; defaults to original name)",
)
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def dupeevent(interaction: discord.Interaction, index: int, date: str, time: Optional[str] = None, name: Optional[str] = None):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)

    g = get_guild_state(guild.id)
    sort_events(g)
    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.edit_original_response(content="Invalid index. Use `/listevents`.")
        return

    orig_dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
    use_time = time.strip() if time and time.strip() else orig_dt.strftime("%H:%M")
    use_name = name.strip() if name and name.strip() else ev["name"]

    try:
        dt = datetime.strptime(f"{date.strip()} {use_time}", "%m/%d/%Y %H:%M").replace(tzinfo=DEFAULT_TZ)
    except ValueError:
        await interaction.edit_original_response(content="Invalid date/time. Use MM/DD/YYYY + 24-hour HH:MM.")
        return

    if dt <= datetime.now(DEFAULT_TZ):
        await interaction.edit_original_response(content="That date/time is in the past. Please choose a future time.")
        return

    new_ev = {
        "name": use_name,
        "timestamp": int(dt.timestamp()),
        "milestones": ev.get("milestones", DEFAULT_MILESTONES.copy()).copy(),
        "announced_milestones": [],
        "repeat_every_days": ev.get("repeat_every_days"),
        "repeat_anchor_date": None,
        "announced_repeat_dates": [],
        "silenced": ev.get("silenced", False),
        "owner_user_id": ev.get("owner_user_id"),
        "start_announced": False,
    }

    g["events"].append(new_ev)
    sort_events(g)
    save_state()

    ch_id = g.get("event_channel_id")
    if ch_id:
        ch = await get_text_channel(ch_id)
        if ch:
            await rebuild_pinned_message(guild.id, ch, g)

    await interaction.edit_original_response(content=
        f"🧬 Duplicated event #{index} → added **{new_ev['name']}** on {dt.strftime('%B %d, %Y at %I:%M %p %Z')}."
    )


@bot.tree.command(name="remindall", description="Send a notification to the channel about an event.")
@app_commands.describe(index="Optional: event number from /listevents (defaults to next upcoming event)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def remindall(interaction: discord.Interaction, index: Optional[int] = None):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)
    
    g = get_guild_state(guild.id)
    sort_events(g)

    channel_id = g.get("event_channel_id")
    if not channel_id:
        await interaction.edit_original_response(content="No event channel set. Run `/seteventchannel` first.")
        return

    channel = await get_text_channel(channel_id)
    if channel is None:
        await interaction.edit_original_response(content="I couldn't access the configured event channel.")
        return

    bot_member = await get_bot_member(guild)
    if not bot_member:
        await interaction.edit_original_response(content="I couldn't resolve my own permissions in this server.")
        return

    ev = None
    dt = None
    now = datetime.now(DEFAULT_TZ)

    if index is not None:
        ev = get_event_by_index(g, index)
        if not ev:
            await interaction.edit_original_response(content="Invalid index. Use `/listevents`.")
            return
        dt = datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ)
    else:
        for candidate in g.get("events", []):
            cdt = datetime.fromtimestamp(candidate["timestamp"], tz=DEFAULT_TZ)
            if cdt > now:
                ev = candidate
                dt = cdt
                break

    if not ev or not dt:
        await interaction.edit_original_response(content="No upcoming event found to remind about.")
        return

    if ev.get("silenced", False):
        await interaction.edit_original_response(content="That event is currently silenced (use `/silence` to toggle it back on).")
        return

    desc, _, passed = compute_time_left(dt)
    if passed:
        await interaction.edit_original_response(content="That event has already started or passed.")
        return

    perms = channel.permissions_for(bot_member)
    mention_prefix = ""
    allowed = discord.AllowedMentions.none()

    if perms.mention_everyone:
        mention_prefix, allowed = build_everyone_mention()
    else:
        mention_prefix, allowed = build_milestone_mention(channel, g)

    date_str = dt.strftime("%B %d, %Y at %I:%M %p %Z")
    msg = f"{mention_prefix}⏰ Reminder: **{ev['name']}** is in **{desc}** (on **{date_str}**)."

    try:
        await channel.send(msg, allowed_mentions=allowed)
    except discord.Forbidden:
        missing = missing_channel_perms(channel, channel.guild)
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=missing,
            action="send /remindall notifications",
        )
        await interaction.edit_original_response(content=
            "I don't have permission to send messages in the event channel."
        )
        return

    await interaction.edit_original_response(content="✅ Reminder sent.")


@bot.tree.command(name="setmilestones", description="Set custom milestone days for an event.")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    milestones="Comma/space-separated days (example: 100, 50, 30, 14, 7, 2, 1, 0)",
)
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setmilestones(interaction: discord.Interaction, index: int, milestones: str):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents`.", ephemeral=True)
        return

    parsed = parse_milestones(milestones)
    if parsed is None or len(parsed) == 0:
        await interaction.response.send_message("Invalid milestones. Use numbers like: `100, 50, 30, 14, 7, 2, 1, 0`.", ephemeral=True)
        return

    ev["milestones"] = parsed
    ev["announced_milestones"] = []
    save_state()

    await interaction.response.send_message(
        f"✅ Updated milestones for **{ev['name']}**: {', '.join(str(x) for x in parsed)}",
        ephemeral=True,
    )


@bot.tree.command(name="resetmilestones", description="Restore default milestone days for an event.")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def resetmilestones(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents`.", ephemeral=True)
        return

    ev["milestones"] = DEFAULT_MILESTONES.copy()
    ev["announced_milestones"] = []
    save_state()

    await interaction.response.send_message(
        f"✅ Milestones reset for **{ev['name']}** to defaults: {', '.join(str(x) for x in DEFAULT_MILESTONES)}",
        ephemeral=True,
    )


@bot.tree.command(name="silence", description="Stop reminders for an event (keeps it listed).")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def silence(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents`.", ephemeral=True)
        return

    ev["silenced"] = not bool(ev.get("silenced", False))
    save_state()

    state_word = "silenced 🔕" if ev["silenced"] else "unsilenced 🔔"
    await interaction.response.send_message(
        f"✅ **{ev['name']}** is now {state_word}.",
        ephemeral=True,
    )


@bot.tree.command(name="seteventowner", description="Assign an owner (they get milestone DMs).")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    user="User who should receive DMs for this event",
)
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def seteventowner(interaction: discord.Interaction, index: int, user: discord.User):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents`.", ephemeral=True)
        return

    ev["owner_user_id"] = int(user.id)
    save_state()

    await interaction.response.send_message(
        f"✅ Set owner for **{ev['name']}** to {user.mention} (they'll receive milestone + repeat reminder DMs).",
        ephemeral=True,
    )


@bot.tree.command(name="cleareventowner", description="Remove the owner for an event.")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def cleareventowner(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message("Invalid index. Use `/listevents`.", ephemeral=True)
        return

    ev["owner_user_id"] = None
    save_state()

    await interaction.response.send_message(
        f"✅ Cleared owner for **{ev['name']}**.",
        ephemeral=True,
    )


@bot.tree.command(name="setmentionrole", description="Mention a role on milestone posts.")
@app_commands.describe(role="Role to mention when milestone reminders post")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setmentionrole(interaction: discord.Interaction, role: discord.Role):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    g["mention_role_id"] = int(role.id)
    save_state()

    await interaction.response.send_message(
        f"✅ Milestone reminders will now mention {role.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="clearmentionrole", description="Stop role mentions on milestone posts.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def clearmentionrole(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    g["mention_role_id"] = None
    save_state()

    await interaction.response.send_message(
        "✅ Milestone role mentions have been cleared.",
        ephemeral=True,
    )


@bot.tree.command(name="setrepeat", description="Set a repeating reminder for an event (every X days).")
@app_commands.describe(
    index="The number shown in /listevents (1, 2, 3, ...)",
    every_days="Repeat interval in days (1 = daily, 7 = weekly, etc.)",
)
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def setrepeat(interaction: discord.Interaction, index: int, every_days: int):
    guild = interaction.guild
    assert guild is not None

    if every_days < 1 or every_days > 365:
        await interaction.response.send_message("Repeat interval must be between **1** and **365** days.", ephemeral=True)
        return

    g = get_guild_state(guild.id)
    sort_events(g)
    events = g.get("events", [])

    if not events:
        await interaction.response.send_message("There are no events yet. Add one with `/addevent` first.", ephemeral=True)
        return

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message(f"Index must be between 1 and {len(events)}.", ephemeral=True)
        return

    today = _today_local_date().isoformat()
    ev["repeat_every_days"] = int(every_days)
    ev["repeat_anchor_date"] = today
    ev["announced_repeat_dates"] = []
    save_state()

    plural = "s" if every_days != 1 else ""
    await interaction.response.send_message(
        f"✅ Repeating reminders enabled for **{ev['name']}** — every **{every_days}** day{plural} (starting tomorrow). "
        f"Use `/clearrepeat index: {index}` to turn it off.",
        ephemeral=True,
    )


@bot.tree.command(name="clearrepeat", description="Turn off repeating reminders for an event.")
@app_commands.describe(index="The number shown in /listevents (1, 2, 3, ...)")
@app_commands.autocomplete(index=event_index_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def clearrepeat(interaction: discord.Interaction, index: int):
    guild = interaction.guild
    assert guild is not None

    g = get_guild_state(guild.id)
    sort_events(g)
    events = g.get("events", [])

    if not events:
        await interaction.response.send_message("There are no events to update.", ephemeral=True)
        return

    ev = get_event_by_index(g, index)
    if not ev:
        await interaction.response.send_message(f"Index must be between 1 and {len(events)}.", ephemeral=True)
        return

    ev["repeat_every_days"] = None
    ev["repeat_anchor_date"] = None
    ev["announced_repeat_dates"] = []
    save_state()

    await interaction.response.send_message(f"🧹 Repeating reminders disabled for **{ev['name']}**.", ephemeral=True)


@bot.tree.command(name="archivepast", description="Remove past events.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def archivepast(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    
    await interaction.response.defer(ephemeral=True)
    
    g = get_guild_state(guild.id)
    sort_events(g)

    now = datetime.now(DEFAULT_TZ)
    before = len(g.get("events", []))
    g["events"] = [ev for ev in g.get("events", []) if datetime.fromtimestamp(ev["timestamp"], tz=DEFAULT_TZ) > now]
    after = len(g["events"])
    removed = before - after

    save_state()

    ch_id = g.get("event_channel_id")
    if ch_id:
        ch = await get_text_channel(ch_id)
        if ch:
            await rebuild_pinned_message(guild.id, ch, g)

    await interaction.edit_original_response(content=f"🧹 Archived **{removed}** past event(s).")


@bot.tree.command(name="resetchannel", description="Clear the configured event channel for this server.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def resetchannel(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    g["event_channel_id"] = None
    g["pinned_message_id"] = None
    save_state()

    await interaction.response.send_message(
        "✅ Event channel configuration cleared. Run `/seteventchannel` again to set it.",
        ephemeral=True,
    )


@bot.tree.command(name="healthcheck", description="Show config + permission diagnostics.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def healthcheck(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None
    g = get_guild_state(guild.id)

    channel_id = g.get("event_channel_id")
    mention_role_id = g.get("mention_role_id")
    num_events = len(g.get("events", []))

    lines = []
    lines.append("**ChronoBot Healthcheck**")
    lines.append(f"Server: **{guild.name}**")
    lines.append(f"Events stored: **{num_events}**")

    if channel_id:
        ch = await get_text_channel(channel_id)
        if ch:
            lines.append(f"Event channel: {ch.mention} ✅")
            bot_member = await get_bot_member(guild)
            if bot_member:
                perms = ch.permissions_for(bot_member)
                lines.append(f"• Can view channel: {'✅' if perms.view_channel else '❌'}")
                lines.append(f"• Can send messages: {'✅' if perms.send_messages else '❌'}")
                lines.append(f"• Can embed links: {'✅' if perms.embed_links else '❌'}")
                lines.append(f"• Can read history: {'✅' if perms.read_message_history else '❌'}")
                lines.append(f"• Can manage messages (pin/unpin): {'✅' if perms.manage_messages else '❌'}")
                lines.append(f"• Can mention @everyone: {'✅' if perms.mention_everyone else '❌'}")
            else:
                lines.append("• Bot member resolution: ❌ (couldn’t fetch bot member)")
        else:
            lines.append("Event channel: ❌ (configured channel not found / not accessible)")
    else:
        lines.append("Event channel: ❌ (not set)")

    if mention_role_id:
        role = guild.get_role(int(mention_role_id))
        lines.append(f"Mention role: {role.mention} ✅" if role else "Mention role: ❌ (role not found)")
    else:
        lines.append("Mention role: (none)")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="purgeevents", description="Delete all events for this server (requires confirm).")
@app_commands.describe(confirm="Type YES to confirm you want to delete all events.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def purgeevents(interaction: discord.Interaction, confirm: str):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)
    
    if (confirm or "").strip().upper() != "YES":
        await interaction.edit_original_response(content="Not confirmed. To purge, run `/purgeevents confirm: YES`.")
        return

    g = get_guild_state(guild.id)
    g["events"] = []
    g["pinned_message_id"] = None
    save_state()

    ch_id = g.get("event_channel_id")
    if ch_id:
        ch = await get_text_channel(ch_id)
        if ch:
            await rebuild_pinned_message(guild.id, ch, g)

    await interaction.edit_original_response(content="🧨 All events have been deleted for this server.")


@bot.tree.command(name="update_countdown", description="Force-refresh the pinned countdown.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.guild_only()
async def update_countdown_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)
    
    g = get_guild_state(guild.id)
    sort_events(g)

    channel_id = g.get("event_channel_id")
    if not channel_id:
        await interaction.edit_original_response(content=
            "No events channel set yet. Run `/seteventchannel` first."
        )
        return

    if interaction.channel_id != channel_id:
        await interaction.edit_original_response(content=
            "Please run this command in the configured events channel."
        )
        return

    channel = interaction.channel
    assert isinstance(channel, discord.TextChannel)

    pinned = await get_or_create_pinned_message(guild.id, channel, allow_create=True)
    if pinned is None:
        await interaction.edit_original_response(
            content="I couldn't create or access the pinned countdown message here. Check my permissions.",
        )
        return

    embed = build_embed_for_guild(g)
    try:
        await pinned.edit(embed=embed)
    except discord.Forbidden:
        missing = missing_channel_perms(channel, channel.guild)
        await notify_owner_missing_perms(
            channel.guild,
            channel,
            missing=missing,
            action="edit/update the pinned countdown message",
        )
        await interaction.edit_original_response(content=
            "I don't have permission to edit that pinned message here. "
            "I’ve messaged the server owner with a permissions fix guide.",
        )
        return
    except discord.HTTPException as e:
        await interaction.edit_original_response(content=
            f"Discord errored while updating the pinned message: {e}",
        )
        return

    await interaction.edit_original_response(content="⏱ Countdown updated.")


@bot.tree.command(name="resendsetup", description="Resend the onboarding/setup message.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def resendsetup(interaction: discord.Interaction):
    guild = interaction.guild
    assert guild is not None

    await interaction.response.defer(ephemeral=True)
    
    g = get_guild_state(guild.id)
    g["welcomed"] = False
    save_state()

    await send_onboarding_for_guild(guild)
    await interaction.edit_original_response(content=
        "📨 Setup instructions have been resent to the server owner (or a fallback channel)."
    )


@bot.tree.command(name="chronohelp", description="Show ChronoBot setup & command help.")
async def chronohelp(interaction: discord.Interaction):
    text = (
        "**ChronoBot – Setup & Commands**\n\n"
        "All slash command responses are ephemeral (only you see them).\n\n"
        "**Tip:** Use `/listevents` to see event numbers for any command that needs `index:` "
        "(or just start typing and pick from the autocomplete list).\n\n"
        "**Setup**\n"
        "• `/seteventchannel` – pick the channel where the pinned countdown lives\n"
        "• `/addevent` – add an event (MM/DD/YYYY + 24-hour HH:MM)\n\n"
        "**Browse**\n"
        "• `/listevents` – list events\n"
        "• `/nextevent` – show the next upcoming event\n"
        "• `/eventinfo index:` – details for one event\n\n"
        "**Edit & organize**\n"
        "• `/editevent index:` – edit name/date/time\n"
        "• `/dupeevent index: date:` – duplicate an event (optional time/name)\n"
        "• `/removeevent index:` – delete an event\n\n"
        "**Repeating reminders (every X days)**\n"
        "• `/setrepeat index: every_days:` – turn on repeating reminders (1 = daily, 7 = weekly)\n"
        "• `/clearrepeat index:` – turn repeating reminders off\n\n"
        "**Milestones & notifications**\n"
        "• `/remindall` – send a notification to the channel about an event\n"
        "• `/setmilestones index: milestones:` – set custom milestone days\n"
        "• `/resetmilestones index:` – restore default milestones\n"
        "• `/silence index:` – stop reminders for an event (keeps it listed)\n"
        "• `/seteventowner index: user:` – assign an owner (they get reminder DMs)\n"
        "• `/cleareventowner index:` – remove the owner\n"
        "• `/setmentionrole role:` – @mention a role on milestone posts\n"
        "• `/clearmentionrole` – stop role mentions\n\n"
        "**Maintenance**\n"
        "• Past events auto-delete after they pass ✅\n"
        "• `/archivepast` – manual cleanup (rarely needed now)\n"
        "• `/resetchannel` – clear the configured channel\n"
        "• `/healthcheck` – show config + permission diagnostics\n"
        "• `/purgeevents confirm: YES` – delete all events for this server\n"
        "• `/update_countdown` – force-refresh the pinned countdown\n"
        "• `/resendsetup` – resend setup instructions\n\n"
        "**Optional: DM control**\n"
        "• `/linkserver` – link your DMs to this server (Manage Server required)\n"
        "• Then DM me `/addevent` to add events remotely\n"
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
