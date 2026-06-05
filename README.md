# YouTube Automation

Automation workflow for generating and uploading YouTube Shorts.

## Auto Generate And Upload Schedule

The GitHub Actions workflow `.github/workflows/pipeline.yml` runs automatically **2 times per day** (UTC):

- `12:05` UTC -> `morning`
- `22:05` UTC -> `evening`

> Cadence was reduced from 4x/day to 2x/day on purpose: posting fewer, higher-retention
> videos performs better than flooding the feed with low-watch-time content, which trains
> the algorithm to suppress the channel's reach.

Manual runs are also supported with `workflow_dispatch`, where you can choose a `slot` value.

## Timezone Note

GitHub Actions cron uses **UTC**. If you want these to match your local morning/afternoon/evening/night windows exactly, convert your local times to UTC and update the cron values.
