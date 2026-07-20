# YouTube Automation

Automation workflow for generating and uploading YouTube Shorts.

## Auto Generate And Upload Schedule

The GitHub Actions workflow `.github/workflows/pipeline.yml` runs automatically **4 times per day** (UTC):

- `03:05` UTC -> `night` (viral trends)
- `12:05` UTC -> `morning` (**high-CPM**: AI tools / finance / SaaS)
- `17:05` UTC -> `afternoon` (**high-CPM**: AI tools / finance / SaaS)
- `22:05` UTC -> `evening` (viral trends)

Manual runs are also supported with `workflow_dispatch`, where you can choose a `slot` value.

## Timezone Note

GitHub Actions cron uses **UTC**. If you want these to match your local morning/afternoon/evening/night windows exactly, convert your local times to UTC and update the cron values.
