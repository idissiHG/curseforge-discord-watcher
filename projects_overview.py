#!/usr/bin/env python3
import json
import io
import os
import re
import sys
import zipfile
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

def get_file(mod_id, file_id):
    return cf_get(f"/mods/{mod_id}/files/{file_id}").get("data", {})

def get_file_download_url(mod_id, file_id):
    return cf_get(f"/mods/{mod_id}/files/{file_id}/download-url").get("data", "")

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

def modpack_manifest_info(mod_id, file_info, project_url=""):
    if not file_info:
        return {"mod_count": None, "loader": "", "loader_version": ""}

    detailed = file_info
    if not detailed.get("downloadUrl") and detailed.get("id"):
        try:
            detailed = get_file(mod_id, detailed["id"]) or file_info
        except Exception as e:
            print(f"[WARN] Could not fetch detailed file info for {mod_id}: {e}")

    download_url = detailed.get("downloadUrl")
    if not download_url and detailed.get("id"):
        try:
            download_url = get_file_download_url(mod_id, detailed["id"])
            if download_url:
                print(f"[INFO] Loaded direct CurseForge download URL for {mod_id}: file {detailed['id']}")
        except Exception as e:
            print(f"[WARN] Could not get CurseForge download URL for {mod_id}: {e}")

    if not download_url:
        return {"mod_count": None, "loader": "", "loader_version": ""}

    try:
        req = Request(
            download_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; CurseForgeProjectOverview/1.0)",
                "Accept": "*/*"
            }
        )
        with urlopen(req, timeout=60) as res:
            data = res.read()

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            manifest_name = "manifest.json"
            if manifest_name not in names:
                matches = [n for n in names if n.endswith("/manifest.json")]
                if not matches:
                    return {"mod_count": None, "loader": "", "loader_version": ""}
                manifest_name = matches[0]

            manifest = json.loads(zf.read(manifest_name).decode("utf-8"))
            files = manifest.get("files") or []

            minecraft = manifest.get("minecraft") or {}
            mod_loaders = minecraft.get("modLoaders") or []
            loader_name = ""
            loader_version = ""

            for item in mod_loaders:
                raw = str(item.get("id") or "")
                lower = raw.lower()
                if lower.startswith("neoforge-"):
                    loader_name = "NeoForge"
                    loader_version = raw.split("-", 1)[1]
                    break
                if lower.startswith("forge-"):
                    loader_name = "Forge"
                    loader_version = raw.split("-", 1)[1]
                    break
                if lower.startswith("fabric-"):
                    loader_name = "Fabric"
                    loader_version = raw.split("-", 1)[1]
                    break
                if lower.startswith("quilt-"):
                    loader_name = "Quilt"
                    loader_version = raw.split("-", 1)[1]
                    break

            return {
                "mod_count": len(files),
                "loader": loader_name,
                "loader_version": loader_version
            }
    except Exception as e:
        print(f"[WARN] Could not inspect modpack manifest for {mod_id}: {e}")
        return {"mod_count": None, "loader": "", "loader_version": ""}

def build_embeds(projects, state, now):
    projects_sorted = sorted(projects, key=lambda m: (m.get("name") or "").lower())
    total_downloads = 0
    modpacks = []
    mods = []
    others = []

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
        latest = latest_file(mod)

        if latest:
            mc_versions = file_minecraft_versions(latest)
            loader = file_loader(latest)
            configured_modloader_version = mod.get("_configured_modloader_version", "")

            info_parts = []
            if mc_versions:
                info_parts.append("Minecraft " + ", ".join(mc_versions))
            if kind == "MODPACK":
                pack_info = modpack_manifest_info(mid, latest, url)
                if pack_info.get("loader"):
                    loader = pack_info["loader"]
                if loader:
                    info_parts.append(loader)

                pack_loader_version = pack_info.get("loader_version") or configured_modloader_version
                if pack_loader_version:
                    info_parts.append("Modloader Version " + pack_loader_version)

                count = pack_info.get("mod_count")
                if count is not None:
                    info_parts.append(f"{count} Mods")
            else:
                if loader:
                    info_parts.append(loader)
                if configured_modloader_version:
                    info_parts.append("Modloader Version " + configured_modloader_version)

            info_line = " • ".join(info_parts) if info_parts else "Release information unavailable"
        else:
            info_line = "No release yet"

        entry = (
            f"{prefix}**[{name}]({url})**\n"
            f"*{info_line}*\n"
            f"⬇️ **{fmt_downloads(downloads)}** downloads"
        )

        if kind == "MODPACK":
            modpacks.append(entry)
        elif kind == "MOD":
            mods.append(entry)
        else:
            others.append(entry)

    sections = []

    if modpacks:
        sections.append("## 📦 MODPACKS\n\n" + "\n\n".join(modpacks))

    if mods:
        sections.append("## 🧩 MODS\n\n" + "\n\n".join(mods))

    if others:
        sections.append("## 🔹 OTHER PROJECTS\n\n" + "\n\n".join(others))

    berlin_now = now.astimezone(ZoneInfo("Europe/Berlin"))
    summary_line = (
        f"**Total Projects**: {len(projects_sorted)}"
        f" **• Total Downloads**: {fmt_downloads(total_downloads)}"
        f" **• Last Update:** {berlin_now.strftime('%H:%M')}"
    )

    body = "\n\n".join(sections) if sections else "No projects found."
    body += "\n\n" + summary_line

    chunks = []
    current = ""
    for block in body.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) > 3400 and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)

    embeds = []
    for chunk in chunks:
        embeds.append({
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

    # Merge auto-discovered projects with every explicitly configured project.
    # This keeps projects visible even when CurseForge's primary-author search
    # does not return them yet (for example a newly created project with no files).
    by_id = {str(p.get("id")): p for p in projects if p.get("id") is not None}
    for configured_project in configured:
        mod_id = str(configured_project.get("mod_id", ""))
        if not mod_id.isdigit() or mod_id in by_id:
            continue
        try:
            mod = get_mod(mod_id)
            if mod:
                by_id[mod_id] = mod
                print(f"[INFO] Added configured project missing from author search: {mod.get('name')} ({mod_id})")
        except Exception as e:
            print(f"[WARN] Could not load configured project {mod_id}: {e}")

    config_by_id = {str(p.get("mod_id")): p for p in configured}
    projects = list(by_id.values())
    for mod in projects:
        configured_project = config_by_id.get(str(mod.get("id")), {})
        if configured_project.get("modloader_version"):
            mod["_configured_modloader_version"] = configured_project["modloader_version"]

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
