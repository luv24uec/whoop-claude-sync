# Privacy Policy — whoop-claude-sync

**Last updated:** 2026-08-01

`whoop-claude-sync` is a personal utility that pulls your own Whoop data (recovery, sleep, strain/cycles, workouts, profile) via the official Whoop API and writes it to files on your computer for use with Claude.

## Data we access
With your explicit OAuth consent, the app may read:
- Recovery metrics (score, HRV, resting heart rate)
- Sleep activities
- Cycle / strain data
- Workouts
- Basic profile information

## How data is used
- Data is stored **locally** on your machine (JSONL archive + markdown summaries).
- Data is only sent to Anthropic/Claude if **you** upload the generated files into a Claude Project or otherwise share them.
- The app does not sell data, show ads, or send Whoop data to any third-party server operated by this tool.

## Tokens
OAuth tokens are stored locally under `~/.config/whoop-claude-sync/` with restricted file permissions. You can revoke access anytime from your Whoop account / by deleting the app authorization.

## Contact
Questions about this personal app: use the contact email configured on the Whoop developer app.
