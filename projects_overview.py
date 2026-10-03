#!/usr/bin/env python3
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError

API_BASE = "https://api.curseforge.com/v1"
CONFIG_FILE = "config.json"
STATE_FILE = "projects_overview_state.json"
MINECRAFT_GAME_ID = 432
NEW_HOURS = 24

CF_API_KEY = os.environ.get("CURSEFORGE_API_KEY", "").strip()
WEBHOOK_URL = os.environ.get("DISCORD_PROJECTS_WEBHOOK_URL", "").strip()

def fail(msg):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)

def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {} if default is None else default

def save_json(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

def http_json(url, headers=None, method="GET", body=None):
    req_headers = {
        "Accept": "application/json",
        "User-Agent": "curseforge-project-overview/1.0"
    }
    if headers:
        req_headers.update(headers)
    data = None
    if body is not None:
        req_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = Request(url, headers=req_headers, method=method, data=data)
    try:
        with urlopen(req, timeout=30) as res:
            raw = res.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} for {url}: {details[:500]}")
    except URLError as e:
        raise RuntimeError(f"Network error for {url}: {e}")

def cf_get(path, params=None):
    if not CF_API_KEY:
        fail("GitHub secret CURSEFORGE_API_KEY is missing.")
    url = API_BASE + path
    if params:
        url += "?" + urlencode(params)
    return http_json(url, headers={"x-api-key": CF_API_KEY})

def get_mod(mod_id):
    return cf_get(f"/mods/{mod_id}").get("data", {})

def search_owned_projects(author_id):
    params = {
        "gameId": MINECRAFT_GAME_ID,
        "primaryAuthorId": author_id,
        "pageSize": 50,
        "index": 0
    }
    return cf_get("/mods/search", params).get("data", [])

def detect_primary_author(config_projects):
    known_ids = {int(p["mod_id"]) for p in config_projects if str(p.get("mod_id","")).isdigit()}
    candidates = {}

    for project in config_projects:
        mod_id = str(project.get("mod_id",""))
        if not mod_id.isdigit():
            continue
        try:
            mod = get_mod(mod_id)
        except Exception as e:
            print(f"[WARN] Could not inspect authors for {mod_id}: {e}")
            continue
        for author in mod.get("authors") or []:
            aid = author.get("id")
            if isinstance(aid, int):
                candidates[aid] = author.get("name") or str(aid)

    if not candidates:
        fail("Could not determine a CurseForge author from configured projects.")

    best = None
    for aid, name in candidates.items():
        try:
            projects = search_owned_projects(aid)
        except Exception as e:
            print(f"[WARN] Author lookup failed for {name} ({aid}): {e}")
            continue
        returned_ids = {int(p["id"]) for p in projects if "id" in p}
        overlap = len(known_ids & returned_ids)
        score = (overlap, len(projects))
        print(f"[INFO] Author candidate {name} ({aid}): {overlap}/{len(known_ids)} known projects matched.")
        if best is None or score > best[0]:
            best = (score, aid, name, projects)

    if best is None or best[0][0] == 0:
        fail("Could not identify the primary CurseForge author reliably.")

    _, aid, name, projects = best
    print(f"[INFO] Using CurseForge primary author {name} ({aid}).")
    return aid, name, projects

def parse_dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def fmt_downloads(value):
    try:
        return f"{int(value):,}".replace(",", ".")
    except Exception:
        return "0"

def class_label(mod):
    url = ((mod.get("links") or {}).get("websiteUrl") or "").lower()
    if "/modpacks/" in url:
        return "MODPACK"
    if "/mc-mods/" in url:
        return "MOD"
    return "PROJECT"

def latest_file(mod):
    files = mod.get("latestFiles") or []
    if not files:
        return None
    return max(files, key=lambda f: (f.get("fileDate", ""), int(f.get("id", 0))))

def file_loader(file_info):
    if not file_info:
        return ""
    versions = [str(v) for v in (file_info.get("gameVersions") or [])]
    for loader in ["NeoForge", "Forge", "Fabric", "Quilt"]:
        if any(v.lower() == loader.lower() for v in versions):
            return loader
    return ""

def file_minecraft_versions(file_info):
    if not file_info:
        return []
    versions = [str(v) for v in (file_info.get("gameVersions") or [])]
    excluded = {"neoforge", "forge", "fabric", "quilt", "java"}
    candidates = [v for v in versions if v.lower() not in excluded]
    likely = [v for v in candidates if re.match(r"^\d+(\.\d+){1,3}([\-+].*)?$", v)]
    return likely[:3] if likely else candidates[:3]

def file_version_label(file_info):
    if not file_info:
        return ""
    return file_info.get("displayName") or file_info.get("fileName") or ""

def build_embeds(projects, state, now):
    projects_sorted = sorted(projects, key=lambda m: (m.get("name") or "").lower())
    lines = []
    total_downloads = 0

    for mod in projects_sorted:
        mid = str(mod.get("id"))
        name = mod.get("name") or f"Project {mid}"
        url = ((mod.get("links") or {}).get("websiteUrl") or "https://www.curseforge.com/")
        downloads = int(mod.get("downloadCount") or 0)
        total_downloads += downloads

        meta = state["projects"].get(mid, {})
        is_new = False
        if not meta.get("baseline", False) and meta.get("first_seen"):
            try:
                is_new = now - parse_dt(meta["first_seen"]) < timedelta(hours=NEW_HOURS)
            except Exception:
                pass

        prefix = "🆕 **NEW** • " if is_new else ""
        kind = class_label(mod)
        kind_icon = "🧩" if kind == "MOD" else ("📦" if kind == "MODPACK" else "🔹")

        latest = latest_file(mod)
        if latest:
            mc_versions = file_minecraft_versions(latest)
            loader = file_loader(latest)
            release_version = file_version_label(latest)

            info_parts = []
            if mc_versions:
                info_parts.append("Minecraft " + ", ".join(mc_versions))
            if loader:
                info_parts.append(loader)
            if release_version:
                info_parts.append("Version " + release_version)

            info_line = " • ".join(info_parts) if info_parts else "Release information unavailable"
        else:
            info_line = "No release yet"

        lines.append(
            f"{prefix}{kind_icon} **{kind}** • **[{name}]({url})**\n"
            f"*{info_line}*\n"
            f"⬇️ **{fmt_downloads(downloads)}** downloads"
        )

    berlin_now = now.astimezone(ZoneInfo("Europe/Berlin"))
    summary_line = (
        f"**Total Downloads** : {fmt_downloads(total_downloads)}"
        f"  **•  Last Update: {berlin_now.strftime('%H:%M')}**"
    )

    # Keep enough room for the summary line on the final embed.
    chunks = []
    current = ""
    for entry in lines:
        candidate = entry if not current else current + "\n\n" + entry
        if len(candidate) > 3400:
            chunks.append(current)
            current = entry
        else:
            current = candidate
    if current:
        chunks.append(current)

    if not chunks:
        chunks = ["No projects found."]

    # Add the summary after a blank line at the bottom.
    chunks[-1] = chunks[-1] + "\n\n" + summary_line

    embeds = []
    for i, chunk in enumerate(chunks):
        title = f"📦 My CurseForge Projects ({len(projects_sorted)})"
        if len(chunks) > 1:
            title += f" • {i+1}/{len(chunks)}"
        embeds.append({
            "title": title,
            "description": chunk,
            "color": 0xF16436
        })

    return embeds

def webhook_post(payload):
    if not WEBHOOK_URL:
        fail("GitHub secret DISCORD_PROJECTS_WEBHOOK_URL is missing.")
    url = WEBHOOK_URL + ("&wait=true" if "?" in WEBHOOK_URL else "?wait=true")
    return http_json(url, method="POST", body=payload)

def webhook_patch(message_id, payload):
    if not WEBHOOK_URL:
        fail("GitHub secret DISCORD_PROJECTS_WEBHOOK_URL is missing.")
    url = WEBHOOK_URL.rstrip("/") + f"/messages/{message_id}"
    return http_json(url, method="PATCH", body=payload)

def main():
    config = load_json(CONFIG_FILE, {})
    configured = config.get("projects", [])
    if not configured:
        fail("No projects are configured yet.")

    state = load_json(STATE_FILE, {})
    state.setdefault("projects", {})
    now = datetime.now(timezone.utc)

    author_id = state.get("author_id")
    author_name = state.get("author_name")
    projects = []

    if author_id:
        try:
            projects = search_owned_projects(author_id)
            print(f"[INFO] Loaded {len(projects)} projects for saved author {author_name or author_id}.")
        except Exception as e:
            print(f"[WARN] Saved author lookup failed: {e}")
            author_id = None

    if not author_id:
        author_id, author_name, projects = detect_primary_author(configured)
        state["author_id"] = author_id
        state["author_name"] = author_name

    current_ids = {str(p.get("id")) for p in projects}
    initialized = bool(state.get("initialized"))

    for mod in projects:
        mid = str(mod.get("id"))
        if mid not in state["projects"]:
            state["projects"][mid] = {
                "first_seen": now.isoformat().replace("+00:00", "Z"),
                "baseline": not initialized,
                "name": mod.get("name")
            }
            if initialized:
                print(f"[NEW] New CurseForge project discovered: {mod.get('name')} ({mid})")
            else:
                print(f"[INIT] Baseline project: {mod.get('name')} ({mid})")
        else:
            state["projects"][mid]["name"] = mod.get("name")

    # Keep historical entries so a temporarily hidden/unavailable project doesn't
    # become "NEW" again later.
    state["initialized"] = True
    state["last_check_utc"] = now.isoformat().replace("+00:00", "Z")

    embeds = build_embeds(projects, state, now)
    payload = {
        "username": "CurseForge Projects",
        "embeds": embeds[:10],
        "allowed_mentions": {"parse": []}
    }

    message_id = state.get("discord_message_id")
    if message_id:
        try:
            webhook_patch(message_id, payload)
            print(f"[OK] Updated Discord project overview message {message_id}.")
        except Exception as e:
            print(f"[WARN] Could not edit existing Discord message: {e}")
            result = webhook_post(payload)
            state["discord_message_id"] = result.get("id")
            print(f"[OK] Created replacement Discord project overview message {state['discord_message_id']}.")
    else:
        result = webhook_post(payload)
        state["discord_message_id"] = result.get("id")
        print(f"[OK] Created Discord project overview message {state['discord_message_id']}.")

    save_json(STATE_FILE, state)
    print(f"[DONE] {len(projects)} projects listed.")

if __name__ == "__main__":
    main()
