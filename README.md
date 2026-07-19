# Seoul Index

The source code behind **Seoul Index (숫자로 보는 서울)**, [**@seoul-index.bsky.social**](https://bsky.app/profile/seoul-index.bsky.social), a Bluesky bot. Each post is a short set of real statistics drawn from Seoul Open Data, rendered as a card image. A post goes out as a four-post thread: an English card, then a Korean one, each followed by a short reply carrying the clickable source and tags.

The account is written by A.I. and says so in its profile. This repository is published for transparency: The code here is exactly what composes and sends the posts.

## Design principle: accuracy over wit

**Python owns every number.** It harvests the data, formats each value and detects the sharp juxtapositions. A `claude -p` step only *curates* (which lines, in what order, and a neutral opener), lightly rewords English labels and *translates* the labels to Korean. Claude never emits a numeric value: the poster reuses Python's exact value string in both languages, and a digit-guard rejects any Claude-written label that contains a figure's digits. So a hallucinated number cannot reach a post.

## How a post is built

1. **Harvest** a pool of candidate facts from the live and cached data sources (see below). Each fact carries an exact, pre-formatted value.
2. **Select** with `claude -p`: it picks 3 to 4 lines that form a coherent set, preferring to build around one pre-detected pair, and writes a neutral opener plus Korean labels.
3. **Compose**: Python stitches the chosen labels back onto its own exact values, adds the source line and tags, and enforces the character limit. Wording that every line repeats is trimmed here, so the index reads like one: the metric is named on the first line and each later line carries only what differs ("Estimated crowd in Jamsil", then "In Hongdae"), and anything the opener already says is dropped from the lines entirely. English trims the leading run and Korean the trailing one, since Korean puts the head last.
4. **Render and post**: each index is drawn as a card image (the numbers on the card), and the thread goes out as the English card, a reply with its clickable source and tags, the Korean card, then its source reply. Each card's full text is its image alt text.

Category rotation keeps two consecutive posts off the same metric. A topical emoji leads the opener, and per-line emoji are added only where an obvious one fits; a guard rejects any number or keycap emoji so figures stay Python's alone.

## Card images

Each index is rendered to a PNG by `seoul_index_card.py`: the card is laid out in HTML, screenshotted with headless Google Chrome, then cropped to content with Pillow. Colour emoji and Korean text come from the system fonts, and the look is monospace on cream to match the avatar. A caveat that qualifies the numbers rather than credits them ("Crowds are KT-estimated") sits in a muted footnote on the card, next to the figures it applies to; the source credit stays in the reply below, where it can be a real clickable link. If rendering ever fails, the poster falls back to a plaintext thread, so a post always goes out. The pinned methodology thread is built the same way, as prose cards, by `seoul_index_methodology.py`.

## Data sources

- **[Seoul Open Data](https://data.seoul.go.kr)** (CC-BY): live crowd estimates (KT mobile-signal based, disclosed as estimates), air quality, subway and bus boardings, infrastructure counts, and quarterly commercial-district sales.
- **[KOSIS / Statistics Korea](https://kosis.kr)**: national-contrast lines (Seoul's share of the country's population, and the total-fertility-rate gap). Annual figures, credited on their own source line.

Every post hyperlinks its source.

## Files

| File | Purpose |
| --- | --- |
| `seoul_index_post.py` | Harvest, select, compose, render and post one index (English + Korean card thread). |
| `seoul_index_card.py` | Render an index or prose card to a PNG (headless Chrome, cropped with Pillow); the poster falls back to plaintext if it fails. |
| `seoul_index_methodology.py` | Post the pinned methodology / "about" thread as prose cards. |
| `seoul_index_sales.py` | Weekly full scan of the commercial-district sales dataset into `sales_agg.json` (the poster reads this cheaply). |
| `seoul_index_config.example.json` | Template for the gitignored `seoul_index_config.json`. |
| `seoul_index_avatar.svg` | The account avatar. |

## Setup

Requirements: Python 3, the [`atproto`](https://pypi.org/project/atproto/) and [`Pillow`](https://pypi.org/project/pillow/) packages (`pip install atproto pillow`), `curl`, Google Chrome (for headless card rendering), and the [Claude Code CLI](https://claude.com/claude-code) for the `claude -p` selector.

### API keys

The bot uses free keys from two South Korean open-data portals, both set in `seoul_index_config.json`:

- Seoul Open Data (`api_key`): Required. The source for most veins (crowds, air, transport, infrastructure, sales). Register a free account at [data.seoul.go.kr](https://data.seoul.go.kr/) and request a general authentication key (일반인증키). One key works across every Seoul Open Data service the bot calls.
- KOSIS / Statistics Korea (`kosis_key`): Needed only for the national-contrast lines. Register at [kosis.kr](https://kosis.kr/) and request an OpenAPI key at [kosis.kr/openapi](https://kosis.kr/openapi). The key is a base64 string that ends in `=`, so keep the trailing character. Without this key the bot still runs; the national lines simply don't appear.
- `data4library_key`: Optional and unused by the current code (reserved for a books vein that isn't wired up yet). Leave the placeholder as is.

The Bluesky app password and the Claude token are not API keys; they live in the Keychain, not the config (steps below).

### Configuration

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
python3 seoul_index_post.py             # post one index (English + Korean card thread)
```

The live account posts twice a day (12:30 p.m. and 8:30 p.m. KST) via `launchd`, with the sales scan refreshing weekly.

## Licence

This code is released under the [MIT Licence](LICENSE). The Seoul Open Data and KOSIS figures it draws on are used under their respective open-data terms (Seoul is CC-BY, credited on every post).
