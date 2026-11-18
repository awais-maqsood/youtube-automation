# YouTube Automation

Automation workflow for generating and uploading YouTube Shorts (then Rumble, Instagram, Facebook when configured).

## Auto Generate And Upload Schedule

The GitHub Actions workflow `.github/workflows/pipeline.yml` runs automatically **2 times per day** (UTC):

- `12:05` UTC -> `morning`
- `22:05` UTC -> `evening`

Every slot produces an English **tech / AI / finance** explainer (25-45s), grounded in
real news headlines for the chosen topic. Set the repo variable `CHANNEL_NICHE=viral`
to go back to mixed trending topics.

GitHub often starts scheduled runs late; the slot is taken from the cron entry that
fired, not from the clock.

Manual runs are also supported with `workflow_dispatch`, where you can choose a `slot` value.

## Timezone Note

GitHub Actions cron uses **UTC**. If you want these to match your local morning/afternoon/evening/night windows exactly, convert your local times to UTC and update the cron values.
