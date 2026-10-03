# CurseForge Comment Bot

This service handles the interactive Discord side of the CurseForge comment workflow.

## Status flow

- New comment -> `🆕`
- **In Progress** -> `🟡 In Progress`
- **Done** -> 60-second pending period
- Click **Done** again during the pending period -> restore the previous status
- Pending period expires -> copy the comment to the archive channel and delete the active message

The Done button displays the remaining cooldown and is refreshed in short intervals.

## Button permissions

A Discord member may use the buttons when either:

- their user ID is in `COMMENT_ALLOWED_USER_IDS`, or
- one of their role IDs is in `COMMENT_ALLOWED_ROLE_IDS`.

Unauthorized clicks receive an ephemeral permission message.

## Required environment variables

Copy `.env.comments.example` as a reference.

- `DISCORD_BOT_TOKEN`
- `DISCORD_COMMENTS_CHANNEL_ID`
- `DISCORD_ARCHIVE_CHANNEL_ID`
- `COMMENT_ALLOWED_USER_IDS` and/or `COMMENT_ALLOWED_ROLE_IDS`

Keep the bot token outside GitHub source files. Store it as a secret on the machine/service running the bot.

## Test command

After the bot is online, an authorized user can send:

`!comment_test`

This creates a temporary comment so the buttons, permissions, cooldown and archive flow can be tested before CurseForge comment ingestion is connected.

## Hosting

This bot must stay online to receive button interactions. GitHub Actions is still useful for scheduled jobs, but it is not a suitable permanent host for an interactive Discord bot.

The next integration step is the CurseForge comment collector, which will call `publish_comment(...)` whenever a new project comment is detected.
