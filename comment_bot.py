#!/usr/bin/env python3
import asyncio
import json
import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError

import discord
from dotenv import load_dotenv
from discord.ext import commands, tasks
from discord import app_commands

load_dotenv()

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
MAIN_CHANNEL_ID = int(os.environ.get("DISCORD_COMMENTS_CHANNEL_ID", "0") or 0)
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "279789363825737728") or 0)
GUILD_OBJ = discord.Object(id=GUILD_ID) if GUILD_ID else None
CF_API_KEY = os.environ.get("CURSEFORGE_API_KEY", "").strip()
COMMENT_POLL_SECONDS = int(os.environ.get("COMMENT_POLL_SECONDS", "60") or 60)
IGNORE_CF_AUTHORS = {
    x.strip().lower()
    for x in os.environ.get("COMMENT_IGNORE_AUTHOR_NAMES", "IDiSSi").split(",")
    if x.strip()
}

KNOWN_PROJECTS = [
    {"id": 1716068, "name": "Applied Energistics 2 - Unofficial Port", "type": "MOD", "url": "https://www.curseforge.com/minecraft/mc-mods/applied-energistics-2-unofficial-port"},
    {"id": 1381868, "name": "DyssiCore", "type": "MODPACK", "url": "https://www.curseforge.com/minecraft/modpacks/dyssicore"},
    {"id": 1714863, "name": "DyssiCore NF", "type": "MODPACK", "url": "https://www.curseforge.com/minecraft/modpacks/dyssicore-nf"},
    {"id": 1716091, "name": "FramedBlocks - Unofficial Port", "type": "MOD", "url": "https://www.curseforge.com/minecraft/mc-mods/framedblocks-unofficial-port"},
    {"id": 1716016, "name": "GuideME - Unofficial Port", "type": "MOD", "url": "https://www.curseforge.com/minecraft/mc-mods/guideme-unofficial-port"},
    {"id": 1718239, "name": "Mantle - Unofficial Community Port", "type": "MOD", "url": "https://www.curseforge.com/minecraft/mc-mods/mantle-unofficial-community-port"},
    {"id": 1716123, "name": "PamTreeWood - Expanded", "type": "MOD", "url": "https://www.curseforge.com/minecraft/mc-mods/pamtreewood-expanded"},
]

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
    project_url TEXT,
    comment_url TEXT,
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
CREATE TABLE IF NOT EXISTS seen_cf_comments (
    comment_id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    seen_at INTEGER NOT NULL
);
""")
db.commit()

try:
    db.execute("ALTER TABLE comments ADD COLUMN project_type TEXT NOT NULL DEFAULT 'PROJECT'")
    db.commit()
except sqlite3.OperationalError:
    pass

for column_sql in (
    "ALTER TABLE comments ADD COLUMN project_url TEXT",
    "ALTER TABLE comments ADD COLUMN comment_url TEXT",
):
    try:
        db.execute(column_sql)
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
    project_url: str = ""
    comment_url: str = ""
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

def project_icon(project_type):
    kind = str(project_type or "PROJECT").upper()
    return {
        "MOD": "🧩",
        "MODPACK": "📦",
        "PROJECT": "🔹"
    }.get(kind, "🔹")

def make_embed(row):
    title = f"{project_icon(row['project_type'])} {row['project_name']} - {display_date(row)} - {status_text(row['status'])}"
    comment_url = (row["comment_url"] or row["source_url"] or row["project_url"] or "").strip()

    embed = discord.Embed(
        title=title,
        url=comment_url or None,
        description=f"**{row['author_name']}**\n\n{row['body']}",
        color=0xF16436
    )
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
        "project_url": row["project_url"] or "",
        "comment_url": row["comment_url"] or "",
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
        f"{project_icon(entry.get('project_type') or 'PROJECT')} "
        f"{entry.get('project_name') or 'Unknown Project'} - "
        f"{entry.get('created_at') or 'Unknown date'} - DONE"
    )
    comment_url = (
        entry.get("comment_url")
        or entry.get("source_url")
        or entry.get("project_url")
        or ""
    ).strip()
    embed = discord.Embed(
        title=title,
        url=comment_url or None,
        description=f"**{entry.get('author_name') or 'Unknown User'}**\n\n{entry.get('body') or ''}",
        color=0xF16436
    )
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
        (id, project_name, project_type, author_name, body, source_url, project_url, comment_url, created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')""",
        (comment.id, comment.project_name, comment.project_type, comment.author_name, comment.body, comment.source_url, comment.project_url, comment.comment_url, comment.created_at)
    )
    db.commit()

    row = get_comment(comment.id)
    channel = bot.get_channel(MAIN_CHANNEL_ID) or await bot.fetch_channel(MAIN_CHANNEL_ID)
    message = await channel.send(embed=make_embed(row), view=CommentView(comment.id))
    db.execute("UPDATE comments SET discord_message_id=? WHERE id=?", (message.id, comment.id))
    db.commit()
    return message.id


def http_json(url, headers=None):
    req_headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; IDiSSi-CF-Bot/1.0)"
    }
    if headers:
        req_headers.update(headers)
    req = Request(url, headers=req_headers)
    with urlopen(req, timeout=30) as res:
        raw = res.read().decode("utf-8")
        return json.loads(raw) if raw else {}

def cf_api_get(path, params=None):
    if not CF_API_KEY:
        return {}
    url = "https://api.curseforge.com/v1" + path
    if params:
        url += "?" + urlencode(params)
    return http_json(url, {"x-api-key": CF_API_KEY})

def project_kind_from_url(url):
    u = (url or "").lower()
    if "/modpacks/" in u:
        return "MODPACK"
    if "/mc-mods/" in u:
        return "MOD"
    return "PROJECT"

def discover_comment_projects():
    projects = {int(p["id"]): dict(p) for p in KNOWN_PROJECTS}

    if not CF_API_KEY:
        return list(projects.values())

    try:
        author_candidates = {}
        for p in KNOWN_PROJECTS[:3]:
            data = cf_api_get(f"/mods/{p['id']}").get("data") or {}
            for author in data.get("authors") or []:
                aid = author.get("id")
                if isinstance(aid, int):
                    author_candidates[aid] = author.get("name") or str(aid)

        best = None
        for aid, name in author_candidates.items():
            result = cf_api_get("/mods/search", {
                "gameId": 432,
                "primaryAuthorId": aid,
                "pageSize": 50,
                "index": 0
            }).get("data") or []
            overlap = len(set(projects) & {int(m["id"]) for m in result if m.get("id") is not None})
            score = (overlap, len(result))
            if best is None or score > best[0]:
                best = (score, result)

        if best and best[0][0] > 0:
            for mod in best[1]:
                mid = int(mod["id"])
                url = ((mod.get("links") or {}).get("websiteUrl") or "").strip()
                projects[mid] = {
                    "id": mid,
                    "name": mod.get("name") or f"Project {mid}",
                    "type": project_kind_from_url(url),
                    "url": url or projects.get(mid, {}).get("url", "")
                }
    except Exception as exc:
        print(f"[WARN] Project discovery failed; using known projects: {exc}")

    return list(projects.values())

def fetch_cf_comments(project_id):
    url = f"https://www.curseforge.com/api/v1/mods/{int(project_id)}/comments?page=0&size=20"
    return http_json(url).get("data") or []

def flatten_cf_comments(items):
    flat = []
    for item in items:
        flat.append(item)
        for reply in item.get("replies") or []:
            flat.append(reply)
    return flat

def cf_comment_author_name(item):
    author = item.get("author") or {}
    return (author.get("displayName") or author.get("username") or "Unknown User").strip()

def cf_comment_is_own(item):
    author = item.get("author") or {}
    names = {
        str(author.get("displayName") or "").strip().lower(),
        str(author.get("username") or "").strip().lower()
    }
    names.discard("")
    return bool(names & IGNORE_CF_AUTHORS)

def cf_comment_date(ms):
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""

def seen_cf_comment(comment_id):
    return db.execute(
        "SELECT 1 FROM seen_cf_comments WHERE comment_id=?",
        (int(comment_id),)
    ).fetchone() is not None

def mark_cf_comment_seen(comment_id, project_id):
    db.execute(
        "INSERT OR IGNORE INTO seen_cf_comments(comment_id, project_id, seen_at) VALUES(?,?,?)",
        (int(comment_id), int(project_id), int(time.time()))
    )
    db.commit()

async def poll_curseforge_comments_once():
    projects = await asyncio.to_thread(discover_comment_projects)
    baseline = get_meta("cf_comments_initialized") != "1"
    found = 0

    for project in projects:
        try:
            comments = await asyncio.to_thread(fetch_cf_comments, project["id"])
        except Exception as exc:
            print(f"[WARN] Comment fetch failed for {project['name']} ({project['id']}): {exc}")
            continue

        for item in sorted(flatten_cf_comments(comments), key=lambda x: int(x.get("datePosted") or 0)):
            cid = item.get("id")
            if not isinstance(cid, int):
                continue

            if seen_cf_comment(cid):
                continue

            mark_cf_comment_seen(cid, project["id"])

            if cf_comment_is_own(item):
                continue

            if baseline:
                continue

            body = (item.get("text") or "").strip()
            if not body:
                continue

            project_url = (project.get("url") or "").rstrip("/")
            comments_url = project_url + "/comments" if project_url else ""

            await publish_comment(CommentRecord(
                id=f"cf:{project['id']}:{cid}",
                project_name=project.get("name") or f"Project {project['id']}",
                author_name=cf_comment_author_name(item),
                body=body,
                project_type=project.get("type") or "PROJECT",
                project_url=project_url,
                comment_url=comments_url,
                created_at=cf_comment_date(item.get("datePosted"))
            ))
            found += 1

    if baseline:
        set_meta("cf_comments_initialized", "1")
        print(f"[INIT] CurseForge comments baselined for {len(projects)} projects.")
    elif found:
        print(f"[NEW] Posted {found} new CurseForge comment(s) to Discord.")



async def active_discord_message_exists(row):
    message_id = row["discord_message_id"]
    if not message_id:
        return False
    try:
        channel = bot.get_channel(MAIN_CHANNEL_ID) or await bot.fetch_channel(MAIN_CHANNEL_ID)
        await channel.fetch_message(int(message_id))
        return True
    except discord.NotFound:
        return False
    except Exception as exc:
        print(f"[WARN] Could not verify Discord message {message_id}: {exc}")
        return True

async def import_existing_comments_once():
    projects = await asyncio.to_thread(discover_comment_projects)
    imported = 0
    skipped_own = 0
    skipped_existing = 0

    for project in projects:
        try:
            comments = await asyncio.to_thread(fetch_cf_comments, project["id"])
        except Exception as exc:
            print(f"[WARN] Existing comment import failed for {project['name']} ({project['id']}): {exc}")
            continue

        for item in sorted(flatten_cf_comments(comments), key=lambda x: int(x.get("datePosted") or 0)):
            cid = item.get("id")
            if not isinstance(cid, int):
                continue

            if cf_comment_is_own(item):
                skipped_own += 1
                mark_cf_comment_seen(cid, project["id"])
                continue

            comment_key = f"cf:{project['id']}:{cid}"
            existing = get_comment(comment_key)
            if existing:
                # DONE/archived comments intentionally stay archived even if no public
                # Discord message exists anymore.
                if existing["status"] == "archived":
                    skipped_existing += 1
                    mark_cf_comment_seen(cid, project["id"])
                    continue

                # If somebody manually deleted the active Discord message, recreate it.
                if await active_discord_message_exists(existing):
                    skipped_existing += 1
                    mark_cf_comment_seen(cid, project["id"])
                    continue

                db.execute("DELETE FROM comments WHERE id=?", (comment_key,))
                db.commit()
                print(f"[INFO] Recreating manually deleted Discord comment {comment_key}.")

            body = (item.get("text") or "").strip()
            if not body:
                mark_cf_comment_seen(cid, project["id"])
                continue

            project_url = (project.get("url") or "").rstrip("/")
            comments_url = project_url + "/comments" if project_url else ""

            await publish_comment(CommentRecord(
                id=comment_key,
                project_name=project.get("name") or f"Project {project['id']}",
                author_name=cf_comment_author_name(item),
                body=body,
                project_type=project.get("type") or "PROJECT",
                project_url=project_url,
                comment_url=comments_url,
                created_at=cf_comment_date(item.get("datePosted"))
            ))
            mark_cf_comment_seen(cid, project["id"])
            imported += 1

    set_meta("cf_comments_initialized", "1")
    return imported, skipped_own, skipped_existing

@tasks.loop(seconds=COMMENT_POLL_SECONDS)
async def curseforge_comment_poller():
    try:
        await poll_curseforge_comments_once()
    except Exception as exc:
        print(f"[WARN] CurseForge comment poll failed: {exc}")

@curseforge_comment_poller.before_loop
async def before_curseforge_comment_poller():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"[OK] Logged in as {bot.user} ({bot.user.id})")

    bot.add_view(ArchiveView())
    await ensure_archive_message()

    if not curseforge_comment_poller.is_running():
        curseforge_comment_poller.start()

    try:
        if GUILD_ID and GUILD_OBJ:
            # Remove any old global registrations from Discord.
            bot.tree.clear_commands(guild=None)
            cleared_global = await bot.tree.sync()
            print(f"[OK] Cleared global slash commands ({len(cleared_global)} remaining)")

            # Sync only the guild-specific commands.
            synced = await bot.tree.sync(guild=GUILD_OBJ)
            print(f"[OK] Synced {len(synced)} guild-only slash commands to {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"[OK] Synced {len(synced)} global slash commands")
    except Exception as exc:
        print(f"[WARN] Slash command sync failed: {exc}")

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

def interaction_allowed(interaction: discord.Interaction) -> bool:
    if interaction.user.id in ALLOWED_USER_IDS:
        return True
    roles = getattr(interaction.user, "roles", [])
    return any(
        getattr(role, "id", 0) in ALLOWED_ROLE_IDS
        or getattr(role, "name", "").lower() in ALLOWED_ROLE_NAMES
        for role in roles
    )

@bot.tree.command(name="comment_test", description="Create a test CurseForge comment")
@app_commands.guilds(GUILD_OBJ)
async def slash_comment_test(interaction: discord.Interaction):
    if not interaction_allowed(interaction):
        return await interaction.response.send_message(
            "You don't have permission to manage CurseForge comments.",
            ephemeral=True
        )

    test_id = f"test-{int(time.time())}"
    await publish_comment(CommentRecord(
        id=test_id,
        project_name="CurseForge Comment Test",
        author_name=interaction.user.display_name,
        project_type="MOD",
        body="This is a test comment for the In Progress / Done workflow.",
        source_url="",
        created_at=datetime.now(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
    ))
    await interaction.response.send_message("✅ Test comment created.", ephemeral=True)

@bot.tree.command(name="archive_clear", description="Clear all archived CurseForge comments")
@app_commands.guilds(GUILD_OBJ)
async def slash_archive_clear(interaction: discord.Interaction):
    if not interaction_allowed(interaction):
        return await interaction.response.send_message(
            "You don't have permission to manage CurseForge comments.",
            ephemeral=True
        )

    set_meta("archive_entries", "[]")
    await ensure_archive_message()
    await interaction.response.send_message("✅ Archive cleared.", ephemeral=True)

@bot.tree.command(name="archive_delete", description="Delete one archived CurseForge comment by ID")
@app_commands.guilds(GUILD_OBJ)
@app_commands.describe(public_id="Archive ID, e.g. CF-7A3F91C2")
async def slash_archive_delete(interaction: discord.Interaction, public_id: str):
    if not interaction_allowed(interaction):
        return await interaction.response.send_message(
            "You don't have permission to manage CurseForge comments.",
            ephemeral=True
        )

    target = public_id.strip().upper()
    entries = get_archive_entries()
    kept = []
    removed = False

    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("public_id", "")).upper() == target:
            removed = True
            continue
        kept.append(entry)

    if not removed:
        return await interaction.response.send_message(
            f"Archive ID `{target}` not found.",
            ephemeral=True
        )

    set_meta("archive_entries", json.dumps(kept, ensure_ascii=False))
    await interaction.response.send_message(
        f"✅ `{target}` was removed from the archive.",
        ephemeral=True
    )



@bot.tree.command(name="comments_import_existing", description="Import existing CurseForge comments once")
@app_commands.guilds(GUILD_OBJ)
async def slash_comments_import_existing(interaction: discord.Interaction):
    if not interaction_allowed(interaction):
        return await interaction.response.send_message(
            "You don't have permission to manage CurseForge comments.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    try:
        imported, skipped_own, skipped_existing = await import_existing_comments_once()
        await interaction.followup.send(
            f"✅ Existing comments imported: {imported}\n"
            f"Skipped own replies: {skipped_own}\n"
            f"Already present: {skipped_existing}",
            ephemeral=True
        )
    except Exception as exc:
        await interaction.followup.send(
            f"❌ Existing comment import failed: {exc}",
            ephemeral=True
        )

@bot.tree.command(name="comments_check", description="Check CurseForge comments now")
@app_commands.guilds(GUILD_OBJ)
async def slash_comments_check(interaction: discord.Interaction):
    if not interaction_allowed(interaction):
        return await interaction.response.send_message(
            "You don't have permission to manage CurseForge comments.",
            ephemeral=True
        )
    await interaction.response.defer(ephemeral=True)
    try:
        await poll_curseforge_comments_once()
        await interaction.followup.send("✅ CurseForge comments checked.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ Comment check failed: {exc}", ephemeral=True)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is missing.")
    if not MAIN_CHANNEL_ID:
        raise SystemExit("DISCORD_COMMENTS_CHANNEL_ID is required.")
    if not ALLOWED_USER_IDS and not ALLOWED_ROLE_IDS and not ALLOWED_ROLE_NAMES:
        raise SystemExit("At least one allowed Discord user ID, role ID, or role name is required.")
    bot.run(TOKEN)
