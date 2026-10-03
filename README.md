# CurseForge → Discord Update Watcher

Checks configured CurseForge projects every **15 minutes** using GitHub Actions.

The repository now contains two Discord workflows:

- **CurseForge → Discord** — announces new CurseForge files/releases.
- **CurseForge Project Overview** — maintains a single Discord overview message with all detected projects and current download counts.

## Secrets

In GitHub go to:

**Settings → Secrets and variables → Actions**

Create these repository secrets:

- `CURSEFORGE_API_KEY`
- `DISCORD_WEBHOOK_URL` — release/update channel
- `DISCORD_PROJECTS_WEBHOOK_URL` — project overview channel

Never put API keys or webhook URLs into committed files.

## Release watcher

`watcher.py` checks for new project files every 15 minutes.

The first existing release is used as a baseline. A project that previously had no files will post its first published file as a new release.

## Project overview

`projects_overview.py` automatically identifies the primary CurseForge author from the already configured projects, then loads all Minecraft projects owned by that author.

The Discord message contains:

- project name and CurseForge link
- current project download count
- total project count
- combined downloads
- automatic refresh every 15 minutes
- **🆕 NEW** for 24 hours after a newly discovered project appears

On the first run, all current projects are treated as the baseline, so they are not incorrectly marked as new.

The overview edits the same Discord message instead of posting a new message every 15 minutes.

## Manual test

Go to **Actions** and run either workflow manually with **Run workflow**.
