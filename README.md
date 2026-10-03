# CurseForge → Discord Update Watcher

Checks configured CurseForge projects every **15 minutes** using GitHub Actions.

When a new CurseForge file appears, the workflow posts a Discord embed and remembers the file ID so the same release is not announced twice.

## Setup

1. Edit `config.json` and add your CurseForge project IDs.
2. In GitHub go to **Settings → Secrets and variables → Actions**.
3. Add:
   - `$2a$10$yY7jb0Er/eEHllRqHZIPiurvDXXCYu5uacbvUo265e/1BVMcFeg.m`
   - `https://discord.com/api/webhooks/1555899435580526592/uJM60ror0xRThG2xTJmalScbYwUVgrmgI1jLKW_akNq3zjlpG3sR-800MUEKu8gMsnMz`
4. Go to **Actions → CurseForge → Discord → Run workflow** once manually.

On the first run, the watcher only records the current latest files and sends no old-release messages.

## Schedule

The workflow uses:

```yaml
cron: "*/15 * * * *"
```

GitHub scheduled workflows may occasionally start slightly later than the nominal 15-minute cadence.
