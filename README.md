# Seoul Index

The source code behind **Seoul Index (숫자로 보는 서울)**, [**@seoul-index.bsky.social**](https://bsky.app/profile/seoul-index.bsky.social), a Bluesky bot. Each post is a short set of real statistics, mostly from Seoul Open Data and otherwise from Statistics Korea or the OECD, rendered as a card image. A post goes out as a four-post thread: an English card, then a Korean one, each followed by a short reply carrying the clickable source and tags.

The account is written by A.I. and says so in its profile. This repository is published for transparency: The code here is exactly what composes and sends the posts.

## Design principle: accuracy over wit

**Python owns every number.** It harvests the data, formats each value and detects the sharp juxtapositions. A `claude -p` step only *curates* (which lines, in what order, and a neutral opener), lightly rewords English labels and *translates* the labels to Korean. Claude never emits a numeric value: the poster reuses Python's exact value string in both languages, and a digit-guard rejects any Claude-written label that contains a figure's digits. So a hallucinated number cannot reach a post.

## How a post is built

1. **Harvest** a pool of candidate facts from the live and cached data sources (see below). Each fact carries an exact, pre-formatted value.
2. **Select** with `claude -p`: it picks 3 to 4 lines that form a coherent set, preferring to build around one pre-detected pair, and writes a neutral opener plus Korean labels.
3. **Compose**: Python stitches the chosen labels back onto its own exact values, adds the source line and tags, and enforces the character limit. Wording that every line repeats is trimmed here, so the index reads like one: the metric is named on the first line and each later line carries only what differs ("Estimated crowd in Jamsil", then "In Hongdae"), and anything the opener already says is dropped from the lines entirely. English trims the leading run and Korean the trailing one, since Korean puts the head last.
4. **Render and post**: each index is drawn as a card image (the numbers on the card), and the thread goes out as the English card, a reply with its clickable source and tags, the Korean card, then its source reply. Each card's full text is its image alt text.

## Two kinds of card

Most posts set things against each other across the city. One post in three, on average (a coin flip, never two in a row, rather than a fixed cadence), instead drills into **one place read along a clock** — the crowd right now, what that place is usually like at this hour on this weekday, and the busiest and quietest hours ahead — cycling through the curated spots. These are interspersed with the ordinary index cards, not a replacement for them, and a place that does not answer with enough lines falls back to a normal post.

The spotlight card needs no `claude -p` call: its lines are fixed and in order, and their labels carry clock times, which are numbers Python does not hand over to be reworded or translated. Its opener names the place in both languages from the curated list.

It is headed "hour by hour" rather than "today" on purpose. `citydata_ppltn` knows the present and the next 12 hours and nothing else, so the peak and trough are the busiest and quietest hours **ahead**: the morning that already happened is not in the data. The footnote says the later hours are forecasts. The "usual for a Monday at this hour" line is the one figure that escapes that caveat, because it comes from the bot's own logged observations rather than from the forecast — and it simply does not appear until three separate weeks have been recorded.

Category rotation keeps two consecutive posts off the same metric. A topical emoji leads the opener, and per-line emoji are added only where an obvious one fits; a guard rejects any number or keycap emoji so figures stay Python's alone.

## Card images

Each index is rendered to a PNG by `seoul_index_card.py`: the card is laid out in HTML, screenshotted with headless Google Chrome, then cropped to content with Pillow. Colour emoji and Korean text come from the system fonts, and the look is monospace on cream to match the avatar. A caveat that qualifies the numbers rather than credits them ("Crowds are KT-estimated", or "Metro areas, 2023" on a world card) sits in a muted footnote on the card, next to the figures it applies to; the source credit stays in the reply below, where it can be a real clickable link. If rendering ever fails, the poster falls back to a plaintext thread, so a post always goes out. The pinned methodology thread is built the same way, as prose cards, by `seoul_index_methodology.py`.

## Data sources

- **[Seoul Open Data](https://data.seoul.go.kr)** (CC-BY): live crowd estimates (KT mobile-signal based, disclosed as estimates), air quality, subway and bus boardings, infrastructure counts, and quarterly commercial-district sales. Average-bill lines are sales divided by the number of transactions, so they are what one payment came to, not what one person spent: a restaurant bill covers a shared table.
- **[KOSIS / Statistics Korea](https://kosis.kr)**: national-contrast lines (Seoul's share of the country's population, and the total-fertility-rate gap). Annual figures, credited on their own source line.
- **[OECD](https://data-explorer.oecd.org)** (SDMX, no key): Seoul set against eight peer cities — Tokyo, Osaka, Paris, London, New York, Berlin, Madrid and Amsterdam — on green space per person, share of people within a 5-minute walk of a transit stop, summer-night urban heat island, and population density. One publisher measuring every city the same way is the only sort of source a comparison like this can honestly rest on; nine separate city portals would compare definitions rather than cities.

  Two things constrain it. A measure is used only when Seoul and at least two peers report in the **same year**, because mixed vintages are a comparison of survey dates dressed as a comparison of cities. And an OECD functional urban area is not the city: Seoul's is the whole capital region, roughly 24m people, against the 9.6m city the Seoul Open Data and KOSIS lines describe. So a world card carries "Metro areas" and the year in its footnote, under the figures it qualifies. The vein is also rationed — after a world post, world facts leave the pool for three days — because it holds the widest gaps in the pool and would otherwise crowd out the city itself.

- **[MOLIT 실거래가](https://rt.molit.go.kr)** (via [data.go.kr](https://www.data.go.kr)): one month's apartment-market filings — the dearest and cheapest single sales, a record jeonse deposit, and counts of filings citywide, by district, and jeonse against monthly rent. Every line is a filed transaction or a count of them, never a median or an average, and cancelled filings are excluded. Filings are due within 30 days of a contract, so the bot uses the newest month that can no longer grow (two months back) and caches the harvest for the whole month.
- **[KMA](https://data.kma.go.kr)** (기상청, via data.go.kr): daily readings from station 108, the official Seoul station, observing since 1904. Yesterday's high, low and rain, and the last full month set against the same month fifty years earlier: hottest day, wettest day, and counts of days meeting a stated criterion (33°C or more, nights never below 25°C, days never above freezing). Extremes are published rows and the rest is counting — no monthly means or totals, which would be computations rather than published figures.
- **[Korea Airports Corporation](https://www.airport.co.kr)** (via data.go.kr): Gimpo's monthly transport row — passengers and flights, the same month twenty years earlier, and the domestic/international split, each side of which is a published row via the route filter, never a subtraction. A month publishes from the 5th business day of the next.
- **[HIRA](https://opendata.hira.or.kr)** (건강보험심사평가원, via data.go.kr): patients per condition at Seoul care institutions, from adjudicated health-insurance claims, for the newest complete published care year. Two provisos ride on the card footnote: the region is where the institution is, not where the patient lives, and the counts are insurance claims only. The conditions are curated for recognisability — and for honesty: hair loss was cut because insurance covers so little of it that the true figure would read as wrong.
- **[MCST](https://www.mcst.go.kr)** (문화체육관광부's culture-facility survey, served by 한국문화정보원 via data.go.kr): Seoul's museums and galleries — the counts, and each year's most-visited houses, which are published per-facility visitor totals. The survey lags a year (the 2024 edition carries 2023 figures), which the card footnote says.
- **[OpenStreetMap](https://www.openstreetmap.org/copyright)** (ODbL), via Overpass: English names for subway stations and districts. The Seoul feeds return Korean names only, and the English card should be English throughout. Romanising them mechanically would not do it: the official name of 홍대입구 is "Hongik Univ." and of 시청 is "City Hall". OSM carries `name:en` for the whole capital-area network, including the Korail and AREX lines the Seoul Metro datasets leave out. The table is harvested once and committed, so no post depends on Overpass being up.

Every post hyperlinks its source.

## Files

| File | Purpose |
| --- | --- |
| `seoul_index_post.py` | Harvest, select, compose, render and post one index (English + Korean card thread). |
| `seoul_index_card.py` | Render an index or prose card to a PNG (headless Chrome, cropped with Pillow); the poster falls back to plaintext if it fails. |
| `seoul_index_methodology.py` | Post the pinned methodology / "about" thread as prose cards. |
| `seoul_index_sales.py` | Monthly full scan of the commercial-district sales dataset into `sales_agg.json` (the poster reads this cheaply). |
| `seoul_index_crowd_log.py` | Crowd sampler, hourly from 05:00 to 23:00; appends observed readings to `crowd_history.jsonl` so the bot can say what a place is *usually* like. |
| `seoul_index_names_harvest.py` | Regenerate `seoul_index_names_en.json` from OpenStreetMap. Run occasionally: stations open a few times a year. |
| `seoul_index_names_en.json` | Korean → English names for stations and districts, so the English card carries no Hangul. |
| `seoul_index_config.example.json` | Template for the gitignored `seoul_index_config.json`. |
| `seoul_index_avatar.svg` | The account avatar. |

## Setup

Requirements: Python 3, the [`atproto`](https://pypi.org/project/atproto/) and [`Pillow`](https://pypi.org/project/pillow/) packages (`pip install atproto pillow`), `curl`, Google Chrome (for headless card rendering), and the [Claude Code CLI](https://claude.com/claude-code) for the `claude -p` selector.

### API keys

The bot uses free keys from three South Korean open-data portals, all set in `seoul_index_config.json`:

- Seoul Open Data (`api_key`): Required. The source for most veins (crowds, air, transport, infrastructure, sales). Register a free account at [data.seoul.go.kr](https://data.seoul.go.kr/) and request a general authentication key (일반인증키). One key works across every Seoul Open Data service the bot calls.
- KOSIS / Statistics Korea (`kosis_key`): Needed only for the national-contrast lines. Register at [kosis.kr](https://kosis.kr/) and request an OpenAPI key at [kosis.kr/openapi](https://kosis.kr/openapi). The key is a base64 string that ends in `=`, so keep the trailing character. Without this key the bot still runs; the national lines simply don't appear.
- 공공데이터포털 (`data_go_kr_key`): Needed for the apartment-market and weather lines. Register at [data.go.kr](https://www.data.go.kr/); the account gets ONE key, but each API needs its own 활용신청 (instant, 자동승인) before the key works against it — apply for 아파트 매매 실거래가 (15126469), 아파트 전월세 실거래가 (15126474), 지상(종관, ASOS) 일자료 (15059093), 전국공항 수송실적통계 (15158834), 질병정보서비스 (15119055) and 전국문화기반시설총람 (15125097). Without this key the bot still runs; those lines simply don't appear.
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

The live account posts three times a day (8:30 a.m., 12:30 p.m. and 8:30 p.m. KST) via `launchd`, with the crowd sampler running hourly from 05:00 to 23:00 and the sales scan monthly (the sales data is quarterly, so a weekly scan was recomputing a figure that moves four times a year).

## Licence

This code is released under the [MIT Licence](LICENSE). The Seoul Open Data and KOSIS figures it draws on are used under their respective open-data terms (Seoul is CC-BY, credited on every post).
