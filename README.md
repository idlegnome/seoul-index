# Seoul Index

The source code behind **The Seoul Index (숫자로 보는 서울)**, [**@seoul-index.bsky.social**](https://bsky.app/profile/seoul-index.bsky.social), a Bluesky bot. Each post is a short set of real statistics drawn from Seoul Open Data. Posts go out as a thread, an English index followed by a Korean translation as a threaded reply.

The account is written by A.I. and says so in its profile. This repository is published for transparency: The code here is exactly what composes and sends the posts.

## Design principle: accuracy over wit

**Python owns every number.** It harvests the data, formats each value and detects the sharp juxtapositions. A `claude -p` step only *curates* (which lines, in what order, and a neutral opener), lightly rewords English labels and *translates* the labels to Korean. Claude never emits a numeric value: the poster reuses Python's exact value string in both languages, and a digit-guard rejects any Claude-written label that contains a figure's digits. So a hallucinated number cannot reach a post.

## How a post is built

1. **Harvest** a pool of candidate facts from the live and cached data sources (see below). Each fact carries an exact, pre-formatted value.
2. **Select** with `claude -p`: it picks 3 to 4 lines that form a coherent set, preferring to build around one pre-detected pair, and writes a neutral opener plus Korean labels.
3. **Compose**: Python stitches the chosen labels back onto its own exact values, adds the source line and tags, and enforces the character limit.
4. **Post** the English index, then the Korean translation as a threaded reply.

Category rotation keeps two consecutive posts off the same metric.

## Data sources

- **[Seoul Open Data](https://data.seoul.go.kr)** (CC-BY): live crowd estimates (KT mobile-signal based, disclosed as estimates), air quality, subway and bus boardings, infrastructure counts, and quarterly commercial-district sales.
- **[KOSIS / Statistics Korea](https://kosis.kr)**: national-contrast lines (Seoul's share of the country's population, and the total-fertility-rate gap). Annual figures, credited on their own source line.

Every post hyperlinks its source.

## Files

| File | Purpose |
| --- | --- |
| `seoul_index_post.py` | Harvest, select, compose and post one index (English then Korean). |
| `seoul_index_sales.py` | Weekly full scan of the commercial-district sales dataset into `sales_agg.json` (the poster reads this cheaply). |
| `seoul_index_config.example.json` | Template for the gitignored `seoul_index_config.json`. |
| `seoul_index_avatar.svg` | The account avatar. |

## Setup

Requirements: Python 3, the [`atproto`](https://pypi.org/project/atproto/) package (`pip install atproto`), `curl`, and the [Claude Code CLI](https://claude.com/claude-code) for the `claude -p` selector.

1. Copy the config template and fill in your own free API keys:
   ```
   cp seoul_index_config.example.json seoul_index_config.json
   ```
2. Store the Bluesky app password in the macOS Keychain (it is not kept in the config):
   ```
   security add-generic-password -a "your-handle.bsky.social" -s "seoulindex-bluesky" -w
   ```
3. Create a long-lived Claude Code token for the selector:
   ```
   claude setup-token
   ```
   then store it under Keychain account `seoulbot`, service `claude-oauth-token`.

Run it:

```
python3 seoul_index_post.py --dry-run   # harvest, select, compose and print, no post
python3 seoul_index_post.py             # post one index (English then Korean thread)
```

The live account posts twice a day (12:30 p.m. and 8:30 p.m. KST) via `launchd`, with the sales scan refreshing weekly.

## Licence

This code is released under the [MIT Licence](LICENSE). The Seoul Open Data and KOSIS figures it draws on are used under their respective open-data terms (Seoul is CC-BY, credited on every post).
