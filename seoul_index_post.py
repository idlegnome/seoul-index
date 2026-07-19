#!/usr/bin/env python3
"""
Seoul Index (@seoul-index.bsky.social) — "Seoul by the numbers".

A Harper's-Index-style bot: each post is a short set of statistics drawn from
Seoul Open Data, arranged so two numbers sit next to each other for a double-take
(a near-equal "dead heat", or a wide gap). Posts as a thread: an English index,
then a Korean translation as a threaded reply.

Design principle — accuracy over wit:
  Python owns every NUMBER. It harvests the data, formats each value, and detects
  the sharp juxtapositions. The `claude -p` step only CURATES (which lines, what
  order, an opener), lightly rewords English labels for wit, and TRANSLATES labels
  to Korean. Claude never emits a numeric value; the poster reuses Python's exact
  value string in both languages, and rejects any Claude label that contains a
  digit. So a hallucinated figure cannot reach a post.

Freshness:
  Live facts (crowds, air) are pulled at post time. Daily facts (subway/bus) are
  computed at post time but cached per-day in state so the second daily post is
  cheap. Quarterly sales come from sales_agg.json (refreshed weekly by
  seoul_index_sales.py).

Requires (for actual posting, not --dry-run):
  - seoul_index_config.json with {"api_key": "...", "handle": "seoul-index.bsky.social"}
  - the bot's Bluesky app password in the Keychain:
      security add-generic-password -a "seoul-index.bsky.social" -s "seoulindex-bluesky" -w
  - a long-lived claude setup-token in the Keychain (shared, account 'seoulbot')

Usage:
  python3 seoul_index_post.py            # post one index (English -> Korean thread)
  python3 seoul_index_post.py --dry-run  # harvest, select, compose, print — no post
  python3 seoul_index_post.py --now      # alias for a normal immediate post
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from atproto import Client, client_utils, models

HERE = Path(__file__).parent
CONFIG = HERE / 'seoul_index_config.json'
STATE = HERE / 'seoul_index_state.json'
SALES_AGG = HERE / 'sales_agg.json'
KEYCHAIN_SERVICE = 'seoulindex-bluesky'
CLAUDE_TOKEN_ACCOUNT = 'seoulbot'
CLAUDE_TOKEN_SERVICE = 'claude-oauth-token'
CLAUDE_MODEL = 'claude-sonnet-5'  # wit + Korean; easy to change if unavailable

DRY_RUN = '--dry-run' in sys.argv
MAX_POST_CHARS = 285  # buffer under Bluesky's 300-grapheme limit
SEOUL_TZ = ZoneInfo('Asia/Seoul')
SOURCE_URL = 'https://data.seoul.go.kr/'

# How many recently-used line ids / categories to keep off the next post.
RECENT_IDS_KEEP = 24
RECENT_CATS_KEEP = 2

# Curated live-crowd locations (citydata_ppltn AREA_NM, all verified to resolve).
# A mix of packed / quiet / touristy / young so contrasts are available.
CROWD_SPOTS = [
    ('잠실 관광특구', 'Jamsil'),
    ('홍대 관광특구', 'Hongdae'),
    ('강남역', 'Gangnam Station'),
    ('광화문·덕수궁', 'Gwanghwamun'),
    ('여의도한강공원', 'the Yeouido riverbank'),
    ('명동 관광특구', 'Myeongdong'),
    ('이태원 관광특구', 'Itaewon'),
]

# Rotating openers offered to the selector (it may also write its own). Kept
# deliberately neutral — time/place framings, never a punchline. The house style
# is Harper's: the arrangement carries the joke, the opener never gives it away.
OPENERS = [
    ('Seoul by the numbers', '숫자로 보는 서울'),
    ('Seoul, right now', '지금 서울은'),
    ('Seoul today', '오늘의 서울'),
    ('The city, as it stands', '지금 이 도시는'),
    ('Last quarter in Seoul', '지난 분기의 서울'),
    ('Spent last quarter in Seoul', '지난 분기 서울의 지출'),
    ('Average spend per visit in Seoul', '서울, 1회 평균 지출'),
    ("20-somethings in Seoul's crowds, right now", '지금 서울 인파의 20대'),
    ('Seoul on the move', '움직이는 서울'),
    ('From the city’s data', '서울시 데이터에서'),
    ('Seoul in the nation', '전국 속 서울'),
    ('Seoul and the country', '서울과 전국'),
]

TAGS = [('Seoul', 'seoul'), ('서울', '서울')]

# Set by sales_facts() so compose() can add quarter context to the source line
# instead of repeating it on every spending row.
SALES_Q = {'en': None, 'ko': None}


# --- small utilities -------------------------------------------------------

def http_get_json(url):
    for _ in range(3):
        r = subprocess.run(['curl', '-s', '--max-time', '30', url],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                pass
    raise RuntimeError(f'Request failed: {url}')


def keychain_password(account, service):
    r = subprocess.run(['security', 'find-generic-password', '-a', account,
                        '-s', service, '-w'], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f'No Keychain password for account="{account}" service="{service}".\n'
            f'Add it with:\n'
            f'  security add-generic-password -a "{account}" -s "{service}" -w')
    return r.stdout.strip()


def claude_env():
    env = os.environ.copy()
    r = subprocess.run(['security', 'find-generic-password', '-a', CLAUDE_TOKEN_ACCOUNT,
                        '-s', CLAUDE_TOKEN_SERVICE, '-w'], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        env['CLAUDE_CODE_OAUTH_TOKEN'] = r.stdout.strip()
    return env


def grouped(n):
    return f'{int(round(n)):,}'


def won_ko(amount):
    """Korean currency: 653,500,000,000 -> '6,535억 원'; 3.67e12 -> '3조 6,701억 원';
    small per-visit amounts (< 1억) -> '5,441원'."""
    if amount < 1e8:
        return f'{grouped(amount)}원'
    eok = int(round(amount / 1e8))  # 억 = 10^8
    if eok >= 10000:
        jo, rem = divmod(eok, 10000)
        return f'{jo}조 {rem:,}억 원' if rem else f'{jo}조 원'
    return f'{eok:,}억 원'


def won_en(amount):
    """English currency: 6.535e11 -> '₩653.5bn'; 7.79e9 -> '₩7.8bn'; small -> '₩Xm'."""
    if amount >= 1e12:
        return f'₩{amount / 1e12:.2f}tn'
    if amount >= 1e9:
        return f'₩{amount / 1e9:.1f}bn'
    if amount >= 1e6:
        return f'₩{amount / 1e6:.0f}m'
    return f'₩{grouped(amount)}'


def fact(fid, cat, label_en, value_en, value_ko, estimated=False, pair=None, year=None):
    return {'id': fid, 'cat': cat, 'label_en': label_en, 'value_en': value_en,
            'value_ko': value_ko, 'estimated': estimated, 'pair': pair, 'year': year}


# --- harvesters ------------------------------------------------------------

def crowd_facts(api_key):
    """Live crowd estimates for the curated spots + a fullest/quietest contrast."""
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json/citydata_ppltn'
    got = []
    for area, en in CROWD_SPOTS:
        try:
            d = http_get_json(f'{base}/1/1/{_url(area)}')
            r = d['SeoulRtd.citydata_ppltn'][0]
            mid = (int(r['AREA_PPLTN_MIN']) + int(r['AREA_PPLTN_MAX'])) // 2
            got.append({'en': en, 'mid': mid,
                        'visitor': r['NON_RESNT_PPLTN_RATE'],
                        'female': r['FEMALE_PPLTN_RATE'],
                        'twenties': r['PPLTN_RATE_20']})
        except (RuntimeError, KeyError, IndexError, ValueError):
            continue
    facts = []
    for g in got:
        facts.append(fact(f'crowd_{g["en"]}', 'crowd',
                          f'Estimated crowd in {g["en"]} right now',
                          grouped(g['mid']), grouped(g['mid']), estimated=True))
        facts.append(fact(f'visitor_{g["en"]}', 'crowd',
                          f'Estimated share in {g["en"]} who don’t live there',
                          f'{g["visitor"]}%', f'{g["visitor"]}%', estimated=True))
        facts.append(fact(f'twenties_{g["en"]}', 'crowd',
                          f'Share of the {g["en"].removeprefix("the ")} crowd in their twenties',
                          f'{g["twenties"]}%', f'{g["twenties"]}%', estimated=True))
        facts.append(fact(f'female_{g["en"]}', 'crowd',
                          f'Women’s share of the {g["en"].removeprefix("the ")} crowd',
                          f'{g["female"]}%', f'{g["female"]}%', estimated=True))
    # Contrast pair: fullest vs quietest sampled spot.
    if len(got) >= 2:
        full = max(got, key=lambda g: g['mid'])
        quiet = min(got, key=lambda g: g['mid'])
        facts.append(fact('crowd_fullest', 'crowd',
                          f'Estimated crowd packed into {full["en"]} now',
                          grouped(full['mid']), grouped(full['mid']),
                          estimated=True, pair='crowd_gap'))
        facts.append(fact('crowd_quietest', 'crowd',
                          f'Estimated crowd at {quiet["en"]} the same minute',
                          grouped(quiet['mid']), grouped(quiet['mid']),
                          estimated=True, pair='crowd_gap'))
    # Age contrast: youngest vs oldest sampled crowd, by share in their twenties.
    def _tw(g):
        try:
            return float(g['twenties'])
        except (TypeError, ValueError):
            return -1.0
    ages = [g for g in got if _tw(g) >= 0]
    if len(ages) >= 2:
        young = max(ages, key=_tw)
        old = min(ages, key=_tw)
        for g in (young, old):
            facts.append(fact(f'agegap_{g["en"]}', 'crowd',
                              f'Share of the {g["en"].removeprefix("the ")} crowd in their twenties',
                              f'{g["twenties"]}%', f'{g["twenties"]}%',
                              estimated=True, pair='age_gap'))
    return facts


def air_facts(api_key):
    try:
        d = http_get_json(
            f'http://openapi.seoul.go.kr:8088/{api_key}/json/ListAirQualityByDistrictService/1/25/')
        rows = [v for v in d.values() if isinstance(v, dict) and 'row' in v][0]['row']
        vals = [(x.get('MSRSTE_NM') or x.get('SAREA_NM') or x.get('MSRSTN_NM'),
                 float(x['FPM'])) for x in rows
                if str(x.get('FPM', '')).replace('.', '', 1).isdigit()]
        if not vals:
            return []
        worst = max(vals, key=lambda t: t[1])
        return [fact('air_monitors', 'air', 'Air-quality monitors reporting live across Seoul',
                     str(len(vals)), str(len(vals))),
                fact('air_worst', 'air', f'Dirtiest fine-dust reading right now ({worst[0]})',
                     f'{worst[1]:.0f} µg/m³', f'{worst[1]:.0f} µg/m³')]
    except (RuntimeError, KeyError, IndexError, ValueError):
        return []


def _latest_daily(api_key, service, day_field_ok):
    """Walk back from today (KST) to the most recent date the service has rows for."""
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json/{service}'
    today = datetime.now(SEOUL_TZ).date()
    for back in range(2, 10):
        day = (today - timedelta(days=back)).strftime('%Y%m%d')
        try:
            d = http_get_json(f'{base}/1/1/{day}')
            body = d.get(service, {})
            if body.get('list_total_count'):
                return day, int(body['list_total_count'])
        except RuntimeError:
            continue
    return None, 0


def transport_facts(api_key, state):
    """Subway + bus daily totals for the latest available date. Cached per-day in
    state so the second post of the day doesn't re-sum ~42 bus pages."""
    day, sub_total_rows = _latest_daily(api_key, 'CardSubwayStatsNew', True)
    if not day:
        return []
    cache = state.get('transport_cache', {})
    if cache.get('date') == day:
        c = cache
    else:
        base = f'http://openapi.seoul.go.kr:8088/{api_key}/json'
        # Subway: one page holds all ~617 stations.
        sd = http_get_json(f'{base}/CardSubwayStatsNew/1/{max(sub_total_rows, 700)}/{day}')
        srows = [x for x in sd['CardSubwayStatsNew']['row'] if x['GTON_TNOPE'].isdigit()]
        sub_total = sum(int(x['GTON_TNOPE']) for x in srows)
        # Busiest station, and quietest *sane* one (drop sub-handful feed artifacts
        # at major stations by ignoring boardings < 10).
        srows.sort(key=lambda x: int(x['GTON_TNOPE']))
        busiest = srows[-1]
        sane = [x for x in srows if int(x['GTON_TNOPE']) >= 10]
        quietest = sane[0] if sane else srows[0]
        # Bus: page through the day.
        bd0 = http_get_json(f'{base}/CardBusStatisticsServiceNew/1/1/{day}')
        btot_rows = int(bd0['CardBusStatisticsServiceNew']['list_total_count'])
        bus_total = 0
        route = {}
        for s in range(1, btot_rows + 1, 1000):
            bd = http_get_json(f'{base}/CardBusStatisticsServiceNew/{s}/{min(s + 999, btot_rows)}/{day}')
            for x in bd.get('CardBusStatisticsServiceNew', {}).get('row', []):
                v = int(x.get('GTON_TNOPE', '0') or 0)
                bus_total += v
                route[x.get('RTE_NM', '?')] = route.get(x.get('RTE_NM', '?'), 0) + v
        top_route = max(route.items(), key=lambda kv: kv[1]) if route else ('?', 0)
        c = {'date': day, 'sub_total': sub_total, 'bus_total': bus_total,
             'busiest_st': busiest['SBWY_STNS_NM'], 'busiest_v': int(busiest['GTON_TNOPE']),
             'quietest_st': quietest['SBWY_STNS_NM'], 'quietest_v': int(quietest['GTON_TNOPE']),
             'top_route': top_route[0], 'top_route_v': top_route[1]}
        state['transport_cache'] = c

    d = datetime.strptime(c['date'], '%Y%m%d').strftime('%-d %b')
    facts = [
        fact('sub_total', 'transport', f'Subway boardings across Seoul on {d}',
             grouped(c['sub_total']), grouped(c['sub_total']), pair='modes'),
        fact('bus_total', 'transport', f'Bus boardings the same day',
             grouped(c['bus_total']), grouped(c['bus_total']), pair='modes'),
        fact('sub_busiest', 'transport', f'Boardings at the busiest station, {c["busiest_st"]}',
             grouped(c['busiest_v']), grouped(c['busiest_v']), pair='station_gap'),
        fact('sub_quietest', 'transport', f'Boardings at the quietest station, {c["quietest_st"]}',
             grouped(c['quietest_v']), grouped(c['quietest_v']), pair='station_gap'),
    ]
    return facts


def count_facts(api_key):
    """Cheap structural counts + cheapest listed cultural event."""
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json'
    out = []

    def total(service):
        d = http_get_json(f'{base}/{service}/1/1/')
        body = [v for v in d.values() if isinstance(v, dict) and 'list_total_count' in v]
        return int(body[0]['list_total_count']) if body else None

    specs = [('wifi', 'TbPublicWifiInfo', 'Public Wi-Fi hotspots the city runs', '공공 와이파이 수'),
             ('library', 'SeoulPublicLibraryInfo', 'Public libraries', None),
             ('park', 'SearchParkInfoService', 'Major parks', None),
             ('busstop', 'busStopLocationXyInfo', 'Bus stops citywide', None),
             ('events', 'culturalEventInfo', 'Cultural events on the city’s listings', None)]
    for fid, service, label, _ in specs:
        try:
            n = total(service)
            if n:
                out.append(fact(f'count_{fid}', 'infra', label, grouped(n), grouped(n),
                                pair='infra' if fid in ('busstop', 'library') else None))
        except (RuntimeError, KeyError, IndexError, ValueError):
            continue
    return out


# Industry categories worth surfacing (Korean name -> English gloss).
SALES_LABELS = {
    '커피-음료': ('coffee shops', '커피-음료'),
    '호프-간이주점': ('pubs and beer halls', '호프-간이주점'),
    '노래방': ('karaoke rooms', '노래방'),
    '치킨전문점': ('fried-chicken shops', '치킨전문점'),
    '서적': ('bookshops', '서적'),
    'PC방': ('internet cafés', 'PC방'),
    '당구장': ('billiard halls', '당구장'),
    '여관': ('motels', '여관'),
    '한식음식점': ('Korean restaurants', '한식음식점'),
    '제과점': ('bakeries', '제과점'),
    '분식전문점': ('snack bars', '분식전문점'),
    '화장품': ('cosmetics shops', '화장품'),
    '편의점': ('convenience stores', '편의점'),
    '애완동물': ('pet shops', '애완동물'),
    '예술학원': ('art academies', '예술학원'),
}


def sales_facts():
    """Latest-quarter industry sales from the cached full scan, with the sharp
    near-equal ('dead heat') pairs pre-detected."""
    if not SALES_AGG.exists():
        return []
    agg = json.loads(SALES_AGG.read_text())
    q = agg.get('latest_quarter')
    inds = agg.get('by_quarter', {}).get(q, {})
    if not inds:
        return []
    SALES_Q['en'] = f'{q[:4]} Q{q[4]}'          # 20261 -> 2026 Q1
    SALES_Q['ko'] = f'{q[:4]}년 {q[4]}분기'      # -> 2026년 1분기
    facts = []
    # Single-industry sales lines for the curated categories. Quarter context
    # lives on the source line (see compose), not repeated on every row.
    for ko, (en, ko_gloss) in SALES_LABELS.items():
        cell = inds.get(ko)
        if not cell:
            continue
        facts.append(fact(f'sales_{ko}', 'spending',
                          f'{en.capitalize()}',
                          won_en(cell['amt']), won_ko(cell['amt'])))
    # Dead-heat detector: any two curated categories within 2% of each other.
    curated = [(ko, inds[ko]['amt']) for ko in SALES_LABELS if ko in inds]
    best = None
    for i in range(len(curated)):
        for j in range(i + 1, len(curated)):
            a, b = curated[i][1], curated[j][1]
            if max(a, b) == 0:
                continue
            gap = abs(a - b) / max(a, b)
            if gap <= 0.02 and (best is None or gap < best[0]):
                best = (gap, curated[i][0], curated[j][0])
    if best:
        _, koa, kob = best
        for ko in (koa, kob):
            en = SALES_LABELS[ko][0]
            facts.append(fact(f'heat_{ko}', 'spending',
                              f'{en.capitalize()}',
                              won_en(inds[ko]['amt']), won_ko(inds[ko]['amt']),
                              pair='dead_heat'))
    # Average-bill (per-transaction spend) facts + the widest gap. A distinct
    # 'avgbill' category so rotation and openers treat it apart from totals.
    avg_list = []
    for ko, (en, ko_gloss) in SALES_LABELS.items():
        cell = inds.get(ko)
        if not cell or not cell.get('co'):
            continue
        avg = cell['amt'] / cell['co']
        avg_list.append((ko, en, avg))
        facts.append(fact(f'avg_{ko}', 'avgbill', en.capitalize(),
                          won_en(avg), won_ko(avg)))
    if len(avg_list) >= 2:
        hi = max(avg_list, key=lambda t: t[2])
        lo = min(avg_list, key=lambda t: t[2])
        for ko, en, avg in (lo, hi):
            facts.append(fact(f'avggap_{ko}', 'avgbill', en.capitalize(),
                              won_en(avg), won_ko(avg), pair='avg_gap'))
    return facts


def _url(s):
    from urllib.parse import quote
    return quote(s)


# --- national contrast (KOSIS / Statistics Korea) --------------------------
# KOSIS is a separate source from data.seoul.go.kr, so compose() credits it on
# its own source line. orgId 101 = Statistics Korea; C1/objL1 00 = 전국 (whole
# country), 11 = 서울특별시. prdSe=Y (annual); newEstPrdCnt=1 takes the latest
# year only. The apiKey must be URL-encoded ('=' -> %3D).

def _kosis_row(key_enc, tbl, itm, obj, prd_se='Y'):
    url = ('https://kosis.kr/openapi/Param/statisticsParameterData.do?method=getList'
           f'&apiKey={key_enc}&format=json&jsonVD=Y&orgId=101&tblId={tbl}'
           f'&itmId={itm}&objL1={obj}&prdSe={prd_se}&newEstPrdCnt=1')
    d = http_get_json(url)
    if isinstance(d, list) and d:
        return d[0]
    raise RuntimeError(f'KOSIS returned no data for {tbl} objL1={obj}: {d!r:.120}')


def kosis_facts(kosis_key):
    """National-vs-Seoul figures from KOSIS: Seoul's share of the country's
    population, and the total-fertility-rate gap (Seoul is the lowest in Korea).
    Annual figures; a KOSIS outage just yields an empty list, never a crash."""
    if not kosis_key:
        return []
    from urllib.parse import quote
    enc = quote(kosis_key, safe='')
    facts = []
    try:
        pop_kr = _kosis_row(enc, 'DT_1B040A3', 'T20', '00')
        pop_se = _kosis_row(enc, 'DT_1B040A3', 'T20', '11')
        n_kr, n_se = int(pop_kr['DT']), int(pop_se['DT'])
        py = pop_se.get('PRD_DE') or None
        facts.append(fact('pop_seoul', 'national', 'People who live in Seoul',
                          grouped(n_se), grouped(n_se), pair='share_gap', year=py))
        facts.append(fact('pop_korea', 'national', 'People who live in South Korea',
                          grouped(n_kr), grouped(n_kr), pair='share_gap', year=py))
        if n_kr:
            share = 100 * n_se / n_kr
            facts.append(fact('pop_share', 'national',
                              'Share of all South Koreans who live in Seoul',
                              f'{share:.1f}%', f'{share:.1f}%', year=py))
    except (RuntimeError, KeyError, IndexError, ValueError, ZeroDivisionError):
        pass
    try:
        fert_kr = _kosis_row(enc, 'DT_1B81A21', 'T1', '00')
        fert_se = _kosis_row(enc, 'DT_1B81A21', 'T1', '11')
        v_kr, v_se = str(fert_kr['DT']), str(fert_se['DT'])
        fy = fert_kr.get('PRD_DE') or None
        facts.append(fact('fert_korea', 'national',
                          'Births the average South Korean woman will have',
                          v_kr, v_kr, pair='fertility_gap', year=fy))
        facts.append(fact('fert_seoul', 'national',
                          'Births the average Seoul woman will have',
                          v_se, v_se, pair='fertility_gap', year=fy))
    except (RuntimeError, KeyError, IndexError, ValueError):
        pass
    return facts


# --- selection + composition ----------------------------------------------

def build_pool(api_key, state, kosis_key=None):
    pool = []
    for fn in (crowd_facts, air_facts):
        pool += fn(api_key)
    pool += transport_facts(api_key, state)
    pool += count_facts(api_key)
    pool += sales_facts()
    pool += kosis_facts(kosis_key)
    return pool


SELECT_PROMPT = """You are the editor of "Seoul by the numbers", a Bluesky account in the style of Harper's Index: a short list of real statistics arranged so two numbers sit next to each other and make the reader do a double-take.

You are given a POOL of candidate lines (each already has an exact value you must NOT change) and some PAIRS that already form a sharp juxtaposition (a near-equal "dead heat", or a wide gap). Build ONE post.

Rules:
- Choose 3 to 4 lines that form a coherent set. STRONGLY prefer building around one PAIR (a dead heat or a wide gap) — that is the joke.
- House style is Harper's Index: let the arrangement carry the joke. NEVER add a line that explains or points out the juxtaposition, and never editorialise. Just the labelled numbers.
- Each line is a bare "Label: value". Do NOT repeat a shared verb or metric on every line — put it once in the opener. For spending posts (₩ amounts), pick an opener that carries the verb, e.g. "Spent last quarter in Seoul", so lines read "Coffee shops: ₩651.4bn", never "Spent at coffee shops: ...".
- Some ₩ lines are per-VISIT averages (category "avgbill"), not quarterly totals. For those use an average-spend opener like "Average spend per visit in Seoul" (never the "Spent last quarter" one), and never mix avgbill lines with quarterly-total spending lines in one post.
- For age-group crowd posts, write the age band as a numeral: "20-somethings" (never "Twentysomethings"). Opener e.g. "20-somethings in Seoul's crowds, right now"; lines are bare place names.
- Do not mix unrelated live "right now" lines with quarterly spending lines in a way that breaks a single frame, unless the contrast itself is the point.
- "national" lines (Seoul set against the whole country: its share of the population, the fertility-rate gap) are annual figures from a different source. Build them into their own "Seoul in the nation" post — never mix a national line with a live "right now" line or a spending line. The fertility pair is only two lines, so pair it with the population-share line to make a set of three.
- Keep the opener neutral (a time or place framing). Pick one from OPENERS, or write a short neutral one (max ~5 words) — it must NOT give away or hint at the pairing. Provide it in English and Korean.
- You may lightly reword an English label for wit, but keep its meaning and DO NOT put any digit in a label.
- Translate every chosen label to natural Korean (labels only — never restate the number in the label).
- Avoid the ids in AVOID_IDS.

Return ONLY JSON:
{"opener_en":"...","opener_ko":"...","note":"one line: what the juxtaposition is","picks":[{"id":"<pool id>","label_en":"<optional reword or copy>","label_ko":"<korean label>"}]}
"""


def select(pool, state):
    avoid = state.get('recent_ids', [])[-RECENT_IDS_KEEP:]
    slim = [{'id': f['id'], 'cat': f['cat'], 'label_en': f['label_en'],
             'value_en': f['value_en'], 'estimated': f['estimated'], 'pair': f['pair']}
            for f in pool]
    pairs = {}
    for f in pool:
        if f['pair']:
            pairs.setdefault(f['pair'], []).append(f['id'])
    payload = {'POOL': slim, 'PAIRS': pairs,
               'OPENERS': [list(o) for o in OPENERS], 'AVOID_IDS': avoid}
    prompt = SELECT_PROMPT + '\n\n' + json.dumps(payload, ensure_ascii=False)
    for attempt in range(2):
        r = subprocess.run(['claude', '-p', '--model', CLAUDE_MODEL, prompt],
                           capture_output=True, text=True, env=claude_env())
        if r.returncode != 0:
            err = (r.stderr or r.stdout or '').strip() or '(no output)'
            raise RuntimeError(f'claude -p failed (exit {r.returncode}): {err}')
        text = re.sub(r'^```[a-z]*\n?|\n?```$', '', r.stdout.strip()).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if attempt == 0:
                continue
            raise RuntimeError(f'claude -p returned invalid JSON: {text[:200]!r}')


def clean_label(label, fallback, value):
    """Accept Claude's label unless it restates the statistic. A label may carry a
    date or year (e.g. 'on 3 Aug'), but if it contains the VALUE's digits it means
    Claude injected the number into the label — reject and use the pool's own
    label so the only source of numbers stays Python."""
    if not label or not label.strip():
        return fallback
    ldigits = re.sub(r'\D', '', label)
    vdigits = re.sub(r'\D', '', value)
    if vdigits and vdigits in ldigits:
        return fallback
    return label.strip()


def clean_opener(text, fallback):
    """Openers carry no statistic, so only sanitise length; a year is fine."""
    if not text or not text.strip():
        return fallback
    return text.strip()[:48]


def compose(sel, pool):
    by_id = {f['id']: f for f in pool}
    picks = [p for p in sel.get('picks', []) if p.get('id') in by_id]
    if len(picks) < 3:
        raise RuntimeError(f'selector returned too few valid picks: {len(picks)}')
    en_lines, ko_lines, used, cats, estimated = [], [], [], set(), False
    for p in picks:
        f = by_id[p['id']]
        label_en = clean_label(p.get('label_en'), f['label_en'], f['value_en'])
        label_ko = clean_label(p.get('label_ko'), f['label_en'], f['value_ko'])
        en_lines.append(f'{label_en}: {f["value_en"]}')
        ko_lines.append(f'{label_ko}: {f["value_ko"]}')
        used.append(f['id'])
        cats.add(f['cat'])
        estimated = estimated or f['estimated']

    opener_en = clean_opener(sel.get('opener_en'), 'Seoul by the numbers')
    opener_ko = clean_opener(sel.get('opener_ko'), '숫자로 보는 서울')

    # Source line credits every distinct source used. Seoul Open Data covers
    # everything except the KOSIS 'national' figures, which get their own credit.
    uses_seoul = any(c != 'national' for c in cats)
    uses_kosis = 'national' in cats
    if uses_seoul and uses_kosis:
        src_en, src_ko = 'Sources: data.seoul.go.kr, kosis.kr', '출처: data.seoul.go.kr, kosis.kr'
    elif uses_kosis:
        src_en, src_ko = 'Source: kosis.kr', '출처: kosis.kr'
    else:
        src_en, src_ko = 'Source: data.seoul.go.kr', '출처: data.seoul.go.kr'
    if ('spending' in cats or 'avgbill' in cats) and SALES_Q['en']:
        src_en += f' · Commercial districts, {SALES_Q["en"]}'
        src_ko += f' · 상권, {SALES_Q["ko"]}'
    if uses_kosis:
        years = sorted({by_id[p['id']].get('year') for p in picks
                        if by_id[p['id']]['cat'] == 'national' and by_id[p['id']].get('year')})
        yr = f', {"/".join(years)}' if years else ''
        src_en += f' · Statistics Korea{yr}'
        src_ko += f' · 통계청{yr}'
    if estimated:
        src_en += ' · crowds KT-estimated'
        src_ko += ' · 인구는 KT 추정'

    cat_list = [by_id[p['id']]['cat'] for p in picks]
    primary = max(set(cat_list), key=cat_list.count)
    en_body = opener_en + ':\n' + '\n'.join(en_lines) + '\n' + src_en
    ko_body = opener_ko + ':\n' + '\n'.join(ko_lines) + '\n' + src_ko
    return en_body, ko_body, used, list(cats), primary


LINK_DOMAINS = [('data.seoul.go.kr', 'https://data.seoul.go.kr'),
                ('kosis.kr', 'https://kosis.kr')]


def add_tags(tb, body):
    # Hyperlink every source domain that appears on the source line.
    hits = sorted((body.find(dom), dom, url) for dom, url in LINK_DOMAINS
                  if body.find(dom) != -1)
    pos = 0
    for i, dom, url in hits:
        if i < pos:  # a later domain nested inside an earlier match — skip
            continue
        tb.text(body[pos:i]).link(dom, url)
        pos = i + len(dom)
    tb.text(body[pos:])
    if TAGS:
        tb.text('\n')
        for i, (tag, label) in enumerate(TAGS):
            if i:
                tb.text(' ')
            tb.tag(f'#{tag}', label)
    return tb


# --- main ------------------------------------------------------------------

def main():
    config = json.loads(CONFIG.read_text())
    api_key = config['api_key']
    kosis_key = config.get('kosis_key')
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    pool = build_pool(api_key, state, kosis_key)
    if len(pool) < 5:
        sys.exit(f'Pool too small ({len(pool)} facts) — data sources may be down.')

    # Category rotation: don't lead with the same metric two posts running.
    last_cat = state.get('last_cat')
    if last_cat:
        rotated = [f for f in pool if f['cat'] != last_cat]
        if len(rotated) >= 5:
            pool = rotated
    print(f'Harvested {len(pool)} candidate facts (rotated away from: {last_cat}).')

    sel = select(pool, state)
    en_body, ko_body, used, cats, primary = compose(sel, pool)

    en_tb = add_tags(client_utils.TextBuilder(), en_body)
    ko_tb = add_tags(client_utils.TextBuilder(), ko_body)
    en_plain, ko_plain = en_tb.build_text(), ko_tb.build_text()

    print(f'\nNote: {sel.get("note", "")}')
    print(f'\nEN ({len(en_plain)} chars):\n{"-"*46}\n{en_plain}\n{"-"*46}')
    print(f'\nKO ({len(ko_plain)} chars):\n{"-"*46}\n{ko_plain}\n{"-"*46}')

    if len(en_plain) > MAX_POST_CHARS or len(ko_plain) > MAX_POST_CHARS:
        sys.exit(f'Post too long (EN {len(en_plain)}, KO {len(ko_plain)}; max {MAX_POST_CHARS}). '
                 f'Re-run to reselect.')

    if DRY_RUN:
        print('\n(dry run — not posting)')
        return

    handle = config['handle']
    password = keychain_password(handle, KEYCHAIN_SERVICE)
    bsky = Client()
    bsky.login(handle, password)
    root = bsky.send_post(text=en_tb, langs=['en'])
    root_ref = models.create_strong_ref(root)
    reply_ref = models.AppBskyFeedPost.ReplyRef(parent=root_ref, root=root_ref)
    bsky.send_post(text=ko_tb, reply_to=reply_ref, langs=['ko'])
    print('\nPosted (English + Korean thread).')

    recent_ids = (state.get('recent_ids', []) + used)[-RECENT_IDS_KEEP:]
    state['recent_ids'] = recent_ids
    state['last_cat'] = primary
    state['last_success_at'] = datetime.now(timezone.utc).isoformat()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
