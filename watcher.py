#!/usr/bin/env python3
import json
import os
import re
import sys
import html as html_lib
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError

API_BASE = "https://api.curseforge.com/v1"
CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

CF_API_KEY = os.environ.get("CURSEFORGE_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

RELEASE_TYPES = {1: "Release", 2: "Beta", 3: "Alpha"}

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
    req_headers = {"Accept": "application/json", "User-Agent": "curseforge-discord-watcher/1.0"}
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

def clean_html(value):
    if not value:
        return ""
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p\s*>", "\n", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = html_lib.unescape(value)
    value = re.sub(r"\r\n?", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()

def trim(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit-1].rstrip() + "…"

def get_mod(mod_id):
    return cf_get(f"/mods/{mod_id}").get("data", {})

def get_latest_file(mod_id):
    files = cf_get(f"/mods/{mod_id}/files", {"pageSize": 50}).get("data", [])
    files = [f for f in files if f.get("isAvailable", True)]
    if not files:
        return None
    return max(files, key=lambda f: (f.get("fileDate", ""), int(f.get("id", 0))))

def get_changelog(mod_id, file_id):
    try:
        return clean_html(cf_get(f"/mods/{mod_id}/files/{file_id}/changelog").get("data", ""))
    except Exception as e:
        print(f"[WARN] Could not load changelog for {mod_id}/{file_id}: {e}")
        return ""

def guess_loader(game_versions):
    values = [str(x) for x in (game_versions or [])]
    for loader in ["NeoForge", "Forge", "Fabric", "Quilt"]:
        if any(loader.lower() == v.lower() for v in values):
            return loader
    return ""

def minecraft_versions(game_versions):
    values = [str(x) for x in (game_versions or [])]
    excluded = {"neoforge","forge","fabric","quilt","java"}
    candidates = [v for v in values if v.lower() not in excluded]
    likely = [v for v in candidates if re.match(r"^\d+(\.\d+){1,3}([\-+].*)?$", v)]
    return likely[:5] if likely else candidates[:5]

def discord_post(payload):
    if not DISCORD_WEBHOOK_URL:
        fail("GitHub secret DISCORD_WEBHOOK_URL is missing.")
    url = DISCORD_WEBHOOK_URL + ("&wait=true" if "?" in DISCORD_WEBHOOK_URL else "?wait=true")
    return http_json(url, method="POST", body=payload)

def build_embed(mod, file_info, changelog, cfg):
    project_name = cfg.get("name") or mod.get("name") or f"CurseForge project {mod.get('id','')}"
    display_name = file_info.get("displayName") or file_info.get("fileName") or str(file_info.get("id"))
    project_url = ((mod.get("links") or {}).get("websiteUrl") or cfg.get("project_url") or "https://www.curseforge.com/")
    logo = mod.get("logo") or {}
    logo_url = logo.get("thumbnailUrl") or logo.get("url")
    versions = file_info.get("gameVersions") or []
    fields = []
    mc = minecraft_versions(versions)
    loader = guess_loader(versions)
    if mc:
        fields.append({"name":"Minecraft","value":", ".join(mc),"inline":True})
    if loader:
        fields.append({"name":"Loader","value":loader,"inline":True})
    fields.append({"name":"Type","value":RELEASE_TYPES.get(file_info.get("releaseType"),"Unknown"),"inline":True})
    fields.append({"name":"Download","value":f"[Open on CurseForge]({project_url})","inline":False})
    desc = f"**{trim(display_name,220)}**"
    if changelog:
        desc += "\n\n**What's new**\n" + trim(changelog,1350)
    color = int(str(cfg.get("embed_color","F16436")).lstrip("#"),16)
    embed = {
        "title": f"🧩 {project_name} updated",
        "url": project_url,
        "description": trim(desc,1900),
        "color": color,
        "fields": fields[:25],
        "footer": {"text":"CurseForge Update Watcher"}
    }
    if logo_url:
        embed["thumbnail"] = {"url": logo_url}
    if file_info.get("fileDate"):
        embed["timestamp"] = file_info["fileDate"]
    return embed

def main():
    config = load_json(CONFIG_FILE,{})
    projects = config.get("projects",[])
    if not projects:
        fail("No projects configured in config.json.")
    state = load_json(STATE_FILE,{})
    state.setdefault("projects",{})
    changed = False
    notifications = 0
    for cfg in projects:
        mod_id = str(cfg.get("mod_id","")).strip()
        if not mod_id.isdigit():
            print(f"[WARN] Skipping invalid mod_id: {mod_id!r}")
            continue
        print(f"[INFO] Checking CurseForge project {mod_id}...")
        try:
            mod = get_mod(mod_id)
            latest = get_latest_file(mod_id)
        except Exception as e:
            print(f"[ERROR] Failed checking {mod_id}: {e}")
            continue
        if not latest:
            print(f"[WARN] No available files found for {mod_id}.")
            continue
        latest_id = int(latest["id"])
        old_id = state["projects"].get(mod_id,{}).get("last_file_id")
        project_name = cfg.get("name") or mod.get("name") or mod_id
        if old_id is None:
            print(f"[INIT] {project_name}: baseline set to file {latest_id}; no Discord post.")
            state["projects"][mod_id] = {
                "last_file_id": latest_id,
                "last_file_date": latest.get("fileDate"),
                "last_display_name": latest.get("displayName") or latest.get("fileName")
            }
            changed = True
            continue
        if latest_id == int(old_id):
            print(f"[OK] {project_name}: no update (file {latest_id}).")
            continue
        if latest_id < int(old_id):
            print(f"[WARN] {project_name}: file {latest_id} older than saved {old_id}; ignoring.")
            continue
        print(f"[NEW] {project_name}: {old_id} -> {latest_id}")
        changelog = get_changelog(mod_id, latest_id)
        try:
            discord_post({
                "username": config.get("discord_username","CurseForge Updates"),
                "avatar_url": config.get("discord_avatar_url") or None,
                "embeds": [build_embed(mod,latest,changelog,cfg)],
                "allowed_mentions":{"parse":[]}
            })
        except Exception as e:
            print(f"[ERROR] Discord post failed for {project_name}: {e}")
            continue
        state["projects"][mod_id] = {
            "last_file_id": latest_id,
            "last_file_date": latest.get("fileDate"),
            "last_display_name": latest.get("displayName") or latest.get("fileName")
        }
        changed = True
        notifications += 1
    if changed:
        state["last_check_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
        save_json(STATE_FILE,state)
    print(f"[DONE] Notifications sent: {notifications}; state changed: {changed}")

if __name__ == "__main__":
    main()
