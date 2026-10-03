#!/usr/bin/env python3
import asyncio
import json
import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass

import discord
from dotenv import load_dotenv
from discord.ext import commands

load_dotenv()

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
MAIN_CHANNEL_ID = int(os.environ.get("DISCORD_COMMENTS_CHANNEL_ID", "0") or 0)

def parse_ids(name):
    raw = os.environ.get(name, "")
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out

ALLOWED_USER_IDS = parse_ids("COMMENT_ALLOWED_USER_IDS")
ALLOWED_ROLE_IDS = parse_ids("COMMENT_ALLOWED_ROLE_IDS")
ALLOWED_ROLE_NAMES = {
    x.strip().lower()
    for x in os.environ.get("COMMENT_ALLOWED_ROLE_NAMES", "").split(",")
    if x.strip()
}

DB_PATH = os.environ.get("COMMENT_DB_PATH", "comments.db")
DONE_DELAY_SECONDS = 60
COUNTDOWN_STEP_SECONDS = 5

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

db.executescript("""
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    project_type TEXT NOT NULL DEFAULT 'PROJECT',
    author_name TEXT NOT NULL,
    body TEXT NOT NULL,
    source_url TEXT,
    created_at TEXT,
    discord_message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'new',
    previous_status TEXT,
    done_deadline INTEGER
);
CREATE TABLE IF NOT EXISTS bot_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
""")
db.commit()

try:
    db.execute("ALTER TABLE comments ADD COLUMN project_type TEXT NOT NULL DEFAULT 'PROJECT'")
    db.commit()
except sqlite3.OperationalError:
    pass

@dataclass
class CommentRecord:
    id: str
    project_name: str
    author_name: str
    body: str
    project_type: str = "PROJECT"
    source_url: str = ""
    created_at: str = ""

def is_allowed(interaction: discord.Interaction) -> bool:
    if interaction.user.id in ALLOWED_USER_IDS:
        return True
    roles = getattr(interaction.user, "roles", [])
    return any(
        getattr(role, "id", 0) in ALLOWED_ROLE_IDS
        or getattr(role, "name", "").lower() in ALLOWED_ROLE_NAMES
        for role in roles
    )

async def deny(interaction: discord.Interaction):
    text = "You don't have permission to manage CurseForge comments."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)

def get_comment(comment_id):
    return db.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()

def short_comment_id(comment_id):
    digest = hashlib.sha1(str(comment_id).encode("utf-8")).hexdigest()[:8].upper()
    return f"CF-{digest}"

def status_text(status):
    return {
        "new": "NEW",
        "progress": "IN PROGRESS",
        "done_pending": "DONE PENDING",
        "archived": "DONE"
    }.get(status, str(status).upper())

def display_date(row):
    value = (row["created_at"] or "").strip()
    return value or "Unknown date"

def make_embed(row):
    project_type = (row["project_type"] or "PROJECT").upper()
    title = f"{project_type} {row['project_name']} - {display_date(row)} - {status_text(row['status'])}"
    embed = discord.Embed(
        title=title,
        description=f"**{row['author_name']}**\n\n{row['body']}",
        color=0xF16436
    )
    if row["source_url"]:
        embed.add_field(name="CurseForge", value=f"[Open comment]({row['source_url']})", inline=False)
    embed.set_footer(text=f"ID: {short_comment_id(row['id'])}")
    return embed

class CommentView(discord.ui.View):
    def __init__(self, comment_id, remaining=None):
        super().__init__(timeout=None)
        self.comment_id = str(comment_id)

        progress = discord.ui.Button(
            label="In Progress",
            style=discord.ButtonStyle.primary,
            custom_id=f"cf_progress:{self.comment_id}"
        )
        progress.callback = self.progress_clicked
        self.add_item(progress)

        done_label = "Done" if remaining is None else f"Done ({remaining}s)"
        done = discord.ui.Button(
            label=done_label,
            style=discord.ButtonStyle.success,
            custom_id=f"cf_done:{self.comment_id}"
        )
        done.callback = self.done_clicked
        self.add_item(done)

    async def progress_clicked(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            return await deny(interaction)

        row = get_comment(self.comment_id)
        if not row or row["status"] == "archived":
            return await interaction.response.send_message("This comment is no longer active.", ephemeral=True)

        db.execute(
            "UPDATE comments SET status='progress', previous_status=NULL, done_deadline=NULL WHERE id=?",
            (self.comment_id,)
        )
        db.commit()
        row = get_comment(self.comment_id)
        await interaction.response.edit_message(embed=make_embed(row), view=CommentView(self.comment_id))

    async def done_clicked(self, interaction: discord.Interaction):
        if not is_allowed(interaction):
            return await deny(interaction)

        row = get_comment(self.comment_id)
        if not row or row["status"] == "archived":
            return await interaction.response.send_message("This comment is no longer active.", ephemeral=True)

        if row["status"] == "done_pending":
            restore = row["previous_status"] or "new"
            db.execute(
                "UPDATE comments SET status=?, previous_status=NULL, done_deadline=NULL WHERE id=?",
                (restore, self.comment_id)
            )
            db.commit()
            row = get_comment(self.comment_id)
            await interaction.response.edit_message(embed=make_embed(row), view=CommentView(self.comment_id))
            return

        previous = row["status"]
        deadline = int(time.time()) + DONE_DELAY_SECONDS
        db.execute(
            "UPDATE comments SET status='done_pending', previous_status=?, done_deadline=? WHERE id=?",
            (previous, deadline, self.comment_id)
        )
        db.commit()
        row = get_comment(self.comment_id)
        await interaction.response.edit_message(
            embed=make_embed(row),
            view=CommentView(self.comment_id, DONE_DELAY_SECONDS)
        )
        asyncio.create_task(done_countdown(self.comment_id, interaction.message.id))

async def done_countdown(comment_id, message_id):
    while True:
        row = get_comment(comment_id)
        if not row or row["status"] != "done_pending" or not row["done_deadline"]:
            return

        remaining = row["done_deadline"] - int(time.time())
        if remaining <= 0:
            await archive_comment(comment_id)
            return

        try:
            channel = bot.get_channel(MAIN_CHANNEL_ID) or await bot.fetch_channel(MAIN_CHANNEL_ID)
            message = await channel.fetch_message(message_id)
            await message.edit(embed=make_embed(row), view=CommentView(comment_id, remaining))
        except discord.NotFound:
            return
        except Exception as exc:
            print(f"[WARN] countdown update failed for {comment_id}: {exc}")

        await asyncio.sleep(min(COUNTDOWN_STEP_SECONDS, max(1, remaining)))

def get_meta(key):
    row = db.execute("SELECT value FROM bot_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None

def set_meta(key, value):
    db.execute(
        "INSERT INTO bot_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )
    db.commit()

def archive_entry(row):
    return {
        "comment_id": row["id"],
        "public_id": short_comment_id(row["id"]),
        "project_name": row["project_name"],
        "project_type": row["project_type"] or "PROJECT",
        "author_name": row["author_name"],
        "body": row["body"],
        "source_url": row["source_url"] or "",
        "created_at": row["created_at"] or "",
        "status": "archived"
    }

def get_archive_entries():
    entries_raw = get_meta("archive_entries") or "[]"
    try:
        entries = json.loads(entries_raw)
        return entries if isinstance(entries, list) else []
    except Exception:
        return []

def archive_embed(entry):
    if isinstance(entry, str):
        return discord.Embed(description=entry, color=0xF16436)

    title = (
        f"{str(entry.get('project_type') or 'PROJECT').upper()} "
        f"{entry.get('project_name') or 'Unknown Project'} - "
        f"{entry.get('created_at') or 'Unknown date'} - DONE"
    )
    embed = discord.Embed(
        title=title,
        description=f"**{entry.get('author_name') or 'Unknown User'}**\n\n{entry.get('body') or ''}",
        color=0xF16436
    )
    if entry.get("source_url"):
        embed.add_field(name="CurseForge", value=f"[Open comment]({entry['source_url']})", inline=False)
    embed.set_footer(text=f"ID: {entry.get('public_id') or 'Unknown'}")
    return embed

class ArchiveView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Archiv anzeigen",
        style=discord.ButtonStyle.secondary,
        custom_id="cf_archive_show"
    )
    async def show_archive(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed(interaction):
            return await deny(interaction)

        entries = get_archive_entries()
        if not entries:
            return await interaction.response.send_message("🗃️ Das Archiv ist aktuell leer.", ephemeral=True)

        embeds = [archive_embed(entry) for entry in entries[-25:]]
        await interaction.response.send_message(
            content=f"🗃️ **ARCHIV** • {len(entries)} Einträge",
            embeds=embeds[:10],
            ephemeral=True
        )
        for i in range(10, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[i:i+10], ephemeral=True)

async def ensure_archive_message():
    channel = bot.get_channel(MAIN_CHANNEL_ID) or await bot.fetch_channel(MAIN_CHANNEL_ID)
    archive_message_id = get_meta("archive_message_id")

    if archive_message_id:
        try:
            msg = await channel.fetch_message(int(archive_message_id))
            await msg.edit(content="## 🗃️ ARCHIV", embed=None, view=ArchiveView())
            return msg
        except discord.NotFound:
            pass

    msg = await channel.send("## 🗃️ ARCHIV", view=ArchiveView())
    set_meta("archive_message_id", msg.id)
    return msg

async def update_archive_message(row):
    entries = get_archive_entries()
    entries.append(archive_entry(row))
    if len(entries) > 250:
        entries = entries[-250:]
    set_meta("archive_entries", json.dumps(entries, ensure_ascii=False))
    await ensure_archive_message()

async def archive_comment(comment_id):
    row = get_comment(comment_id)
    if not row or row["status"] != "done_pending":
        return

    await update_archive_message(row)

    if row["discord_message_id"]:
        try:
            main = bot.get_channel(MAIN_CHANNEL_ID) or await bot.fetch_channel(MAIN_CHANNEL_ID)
            msg = await main.fetch_message(row["discord_message_id"])
            await msg.delete()
        except discord.NotFound:
            pass

    db.execute(
        "UPDATE comments SET status='archived', previous_status=NULL, done_deadline=NULL WHERE id=?",
        (comment_id,)
    )
    db.commit()

async def publish_comment(comment: CommentRecord):
    existing = get_comment(comment.id)
    if existing:
        return existing["discord_message_id"]

    db.execute(
        """INSERT INTO comments
        (id, project_name, project_type, author_name, body, source_url, created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'new')""",
        (comment.id, comment.project_name, comment.project_type, comment.author_name, comment.body, comment.source_url, comment.created_at)
    )
    db.commit()

    row = get_comment(comment.id)
    channel = bot.get_channel(MAIN_CHANNEL_ID) or await bot.fetch_channel(MAIN_CHANNEL_ID)
    message = await channel.send(embed=make_embed(row), view=CommentView(comment.id))
    db.execute("UPDATE comments SET discord_message_id=? WHERE id=?", (message.id, comment.id))
    db.commit()
    return message.id

@bot.event
async def on_ready():
    print(f"[OK] Logged in as {bot.user} ({bot.user.id})")

    bot.add_view(ArchiveView())
    await ensure_archive_message()

    # Restore persistent views and pending countdowns after a restart.
    rows = db.execute(
        "SELECT * FROM comments WHERE status IN ('new','progress','done_pending') AND discord_message_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        remaining = None
        if row["status"] == "done_pending" and row["done_deadline"]:
            remaining = max(0, row["done_deadline"] - int(time.time()))
        bot.add_view(CommentView(row["id"], remaining), message_id=row["discord_message_id"])
        if row["status"] == "done_pending":
            asyncio.create_task(done_countdown(row["id"], row["discord_message_id"]))

@bot.command(name="comment_test")
async def comment_test(ctx):
    # Temporary setup/test helper. Only authorized users can create test comments.
    fake_interaction_user_allowed = (
        ctx.author.id in ALLOWED_USER_IDS or
        any(
            role.id in ALLOWED_ROLE_IDS or role.name.lower() in ALLOWED_ROLE_NAMES
            for role in getattr(ctx.author, "roles", [])
        )
    )
    if not fake_interaction_user_allowed:
        return
    test_id = f"test-{int(time.time())}"
    await publish_comment(CommentRecord(
        id=test_id,
        project_name="CurseForge Comment Test",
        author_name=ctx.author.display_name,
        project_type="MOD",
        body="This is a test comment for the In Progress / Done workflow.",
        source_url="",
        created_at="Test"
    ))

@bot.command(name="archive_delete")
async def archive_delete(ctx, public_id: str = ""):
    allowed = (
        ctx.author.id in ALLOWED_USER_IDS or
        any(
            role.id in ALLOWED_ROLE_IDS or role.name.lower() in ALLOWED_ROLE_NAMES
            for role in getattr(ctx.author, "roles", [])
        )
    )
    if not allowed:
        return

    target = public_id.strip().upper()
    if not target:
        await ctx.reply("Usage: !archive_delete CF-XXXXXXXX")
        return

    entries = get_archive_entries()
    kept = []
    removed = False

    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("public_id", "")).upper() == target:
            removed = True
            continue
        kept.append(entry)

    if not removed:
        await ctx.reply(f"Archive ID {target} not found.")
        return

    set_meta("archive_entries", json.dumps(kept, ensure_ascii=False))
    await ctx.reply(f"✅ {target} was removed from the archive.")

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is missing.")
    if not MAIN_CHANNEL_ID:
        raise SystemExit("DISCORD_COMMENTS_CHANNEL_ID is required.")
    if not ALLOWED_USER_IDS and not ALLOWED_ROLE_IDS and not ALLOWED_ROLE_NAMES:
        raise SystemExit("At least one allowed Discord user ID, role ID, or role name is required.")
    bot.run(TOKEN)
