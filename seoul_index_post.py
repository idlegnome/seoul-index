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
  python3 seoul_index_post.py --spotlight --dry-run   # force the single-place card
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from atproto import Client, client_utils, models

from seoul_index_card import render_card, CardRenderError

HERE = Path(__file__).parent
CONFIG = HERE / 'seoul_index_config.json'
STATE = HERE / 'seoul_index_state.json'
SALES_AGG = HERE / 'sales_agg.json'
KEYCHAIN_SERVICE = 'seoulindex-bluesky'
CLAUDE_TOKEN_ACCOUNT = 'seoulbot'
CLAUDE_TOKEN_SERVICE = 'claude-oauth-token'
CLAUDE_MODEL = 'claude-sonnet-5'  # wit + Korean; easy to change if unavailable

DRY_RUN = '--dry-run' in sys.argv
FORCE_SPOTLIGHT = '--spotlight' in sys.argv   # for testing the single-place card
MAX_POST_CHARS = 285  # buffer under Bluesky's 300-grapheme limit
SEOUL_TZ = ZoneInfo('Asia/Seoul')
SOURCE_URL = 'https://data.seoul.go.kr/'

# How many recently-used line ids / categories to keep off the next post.
RECENT_IDS_KEEP = 24
RECENT_CATS_KEEP = 2

# Curated live-crowd locations (citydata_ppltn AREA_NM, all verified to resolve).
# A mix of packed / quiet / touristy / young so contrasts are available.
# (query name, English name, short Korean name). The query name is the API's own
# AREA_NM and often carries an administrative suffix (관광특구, "special tourist
# zone") that nobody says out loud, so the third field is what a card calls the
# place in Korean.
CROWD_SPOTS = [
    ('잠실 관광특구', 'Jamsil', '잠실'),
    ('홍대 관광특구', 'Hongdae', '홍대'),
    ('강남역', 'Gangnam Station', '강남역'),
    ('광화문·덕수궁', 'Gwanghwamun', '광화문'),
    ('여의도한강공원', 'the Yeouido riverbank', '여의도 한강공원'),
    ('명동 관광특구', 'Myeongdong', '명동'),
    ('이태원 관광특구', 'Itaewon', '이태원'),
]

# One post in every SPOTLIGHT_EVERY drills into a single place instead of
# setting places against each other.
SPOTLIGHT_EVERY = 3

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
    # "Average bill", not "per visit": the figure is sales / number of
    # TRANSACTIONS, so one Korean-restaurant line is a shared table, not one
    # diner. "Per visit" invited the reader to compare it with a coffee, which
    # really is one person paying for themselves.
    ('Average bill in Seoul', '서울의 평균 결제액'),
    ("20-somethings in Seoul's crowds, right now", '지금 서울 인파의 20대'),
    ('Seoul on the move', '움직이는 서울'),
    ('From the city’s data', '서울시 데이터에서'),
    ('Seoul and the nation', '서울과 전국'),
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


def fact(fid, cat, label_en, value_en, value_ko, estimated=False, pair=None,
         year=None, forecast=False, label_ko=None):
    """One candidate line. `label_ko` is normally left None so the selector
    translates the label; spotlight lines set it because their labels carry
    clock times, and a translated time is a number Python no longer owns."""
    return {'id': fid, 'cat': cat, 'label_en': label_en, 'value_en': value_en,
            'value_ko': value_ko, 'estimated': estimated, 'pair': pair,
            'year': year, 'forecast': forecast, 'label_ko': label_ko}


# --- harvesters ------------------------------------------------------------

def crowd_facts(api_key):
    """Live crowd estimates for the curated spots + a fullest/quietest contrast."""
    base = f'http://openapi.seoul.go.kr:8088/{api_key}/json/citydata_ppltn'
    got = []
    for area, en, _ko in CROWD_SPOTS:
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


def _ampm_en(h):
    if h == 0:
        return 'midnight'
    if h == 12:
        return 'noon'
    return f'{h % 12} {"a.m." if h < 12 else "p.m."}'


def _ampm_ko(h):
    if h == 0:
        return '자정'
    if h == 12:
        return '정오'
    return f'{"오전" if h < 12 else "오후"} {h % 12}시'


WEEKDAY_EN = {'Mon': 'Monday', 'Tue': 'Tuesday', 'Wed': 'Wednesday',
              'Thu': 'Thursday', 'Fri': 'Friday', 'Sat': 'Saturday',
              'Sun': 'Sunday'}
WEEKDAY_KO = {'Mon': '월요일', 'Tue': '화요일', 'Wed': '수요일', 'Thu': '목요일',
              'Fri': '금요일', 'Sat': '토요일', 'Sun': '일요일'}


def spotlight_facts(api_key, spot):
    """One place, over time, rather than places against each other.

    Everything here comes from a single citydata_ppltn call plus the bot's own
    accumulated log. The endpoint knows the present and the next 12 hours and
    nothing else, so the peak and trough lines are the busiest and quietest
    hours AHEAD, not of the day: the morning that already happened is not in the
    data, and calling this "today" would claim otherwise. The typical-for-this-
    weekday line comes from crowd_history.jsonl and simply does not appear until
    three separate weeks have been observed.

    Returns facts in reading order (compose keeps it), or [] if the place did
    not answer well enough for a card."""
    area, en, ko = spot
    try:
        d = http_get_json(
            f'http://openapi.seoul.go.kr:8088/{api_key}/json/citydata_ppltn/1/1/{_url(area)}')
        r = d['SeoulRtd.citydata_ppltn'][0]
        now_mid = (int(r['AREA_PPLTN_MIN']) + int(r['AREA_PPLTN_MAX'])) // 2
    except (RuntimeError, KeyError, IndexError, ValueError):
        return []

    stamp = r.get('PPLTN_TIME') or ''
    try:                                   # the reading's own clock, not ours
        now_h = int(stamp[11:13])
    except (ValueError, IndexError):
        now_h = datetime.now(SEOUL_TZ).hour
    wd = datetime.now(SEOUL_TZ).strftime('%a')

    facts = [fact(f'spot_now_{en}', 'spotlight',
                  f'Right now ({_ampm_en(now_h)})',
                  grouped(now_mid), grouped(now_mid), estimated=True,
                  label_ko=f'지금 ({_ampm_ko(now_h)})')]

    # Typical for this weekday and hour, from our own observations. Sits second
    # so it lands next to the live figure it gives meaning to.
    try:
        from seoul_index_crowd_log import baseline
        base = baseline(en, wd, now_h)
    except Exception:                      # no log yet, or unreadable — skip
        base = None
    if base:
        mean, days = base
        facts.append(fact(f'spot_usual_{en}', 'spotlight',
                          f'Usual for a {WEEKDAY_EN.get(wd, wd)} at {_ampm_en(now_h)}',
                          grouped(mean), grouped(mean), estimated=True,
                          label_ko=f'{WEEKDAY_KO.get(wd, wd)} {_ampm_ko(now_h)} 평균'))

    pts = []
    for x in (r.get('FCST_PPLTN') or []):
        try:
            pts.append((int(x['FCST_TIME'][11:13]),
                        (int(x['FCST_PPLTN_MIN']) + int(x['FCST_PPLTN_MAX'])) // 2))
        except (KeyError, ValueError, IndexError):
            continue
    if len(pts) >= 2:
        hi = max(pts, key=lambda p: p[1])
        lo = min(pts, key=lambda p: p[1])
        if hi[0] != lo[0]:                 # a flat forecast says nothing
            facts.append(fact(f'spot_peak_{en}', 'spotlight',
                              f'Busiest hour ahead ({_ampm_en(hi[0])})',
                              grouped(hi[1]), grouped(hi[1]),
                              estimated=True, forecast=True,
                              label_ko=f'가장 붐빌 시간 ({_ampm_ko(hi[0])})'))
            facts.append(fact(f'spot_quiet_{en}', 'spotlight',
                              f'Quietest hour ahead ({_ampm_en(lo[0])})',
                              grouped(lo[1]), grouped(lo[1]),
                              estimated=True, forecast=True,
                              label_ko=f'가장 한산할 시간 ({_ampm_ko(lo[0])})'))
    return facts if len(facts) >= 3 else []


def spotlight_sel(spot, facts):
    """The selector's job on a spotlight card is already done: the lines are
    fixed, in order, and their labels carry clock times that must not be
    reworded or re-translated. So build its answer in Python instead of asking,
    which also spares a claude -p call. The opener names the place in each
    language from CROWD_SPOTS, so nothing needs translating at all."""
    _, en, ko = spot
    place_en = en[0].upper() + en[1:]
    return {
        'opener_en': f'{place_en}, hour by hour',
        'opener_ko': f'{ko}, 시간대별',
        'opener_emoji': '📍',
        'note': f'single-place spotlight: {en}',
        'picks': [{'id': f['id'], 'label_en': f['label_en'],
                   'label_ko': f['label_ko'], 'emoji': ''} for f in facts],
    }


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
- Do NOT worry about line order: when the lines share a unit (e.g. an all-₩ post) they are automatically sorted by value, largest first. A near-equal "dead heat" still lands because near-equal values end up next to each other. Just choose a coherent set.
- Each line is a bare "Label: value". Do NOT repeat a shared verb or metric on every line — put it once in the opener. For spending posts (₩ amounts), pick an opener that carries the verb, e.g. "Spent last quarter in Seoul", so lines read "Coffee shops: ₩651.4bn", never "Spent at coffee shops: ...". This matters for live "right now" lines too: the pool labels repeat the whole phrase ("Estimated crowd in Jamsil right now"), and a post that copies them four times reads like a form. Name the metric on ONE line and leave the others bare ("Estimated crowd in Jamsil", then "Hongdae", "Gangnam Station"), and let the opener carry the time frame.
- Wording shared by EVERY line is trimmed automatically after you answer, so a label you leave repetitive will be cut back rather than posted as-is. Write the labels you want and do not pad them to match each other.
- Some ₩ lines are average BILLS (category "avgbill"), not quarterly totals: sales divided by the number of transactions, i.e. what one payment came to. One bill is not one person — a Korean-restaurant bill covers a shared table, while a coffee is one person paying for themselves. So use an average-bill opener like "Average bill in Seoul" (never the "Spent last quarter" one, and never wording like "per visit" or "per person", which would claim a per-head figure the data does not give). Never mix avgbill lines with quarterly-total spending lines in one post.
- For age-group crowd posts, write the age band as a numeral: "20-somethings" (never "Twentysomethings"). Opener e.g. "20-somethings in Seoul's crowds, right now"; lines are bare place names.
- Do not mix unrelated live "right now" lines with quarterly spending lines in a way that breaks a single frame, unless the contrast itself is the point.
- "national" lines (Seoul set against the whole country: its share of the population, the fertility-rate gap) are annual figures from a different source. Build them into their own "Seoul and the nation" post — never mix a national line with a live "right now" line or a spending line. The fertility pair is only two lines, so pair it with the population-share line to make a set of three.
- Keep the opener neutral (a time or place framing). Pick one from OPENERS, or write a short neutral one (max ~5 words) — it must NOT give away or hint at the pairing. Provide it in English and Korean.
- You may lightly reword an English label for wit, but keep its meaning and DO NOT put any digit in a label.
- Translate every chosen label to natural Korean (labels only — never restate the number in the label).
- Emoji: give "opener_emoji" one topic emoji that fits the whole set. For each pick, give an "emoji" ONLY where an obvious, tasteful one exists (a food, a shop, a place, a clear object). Leave "emoji" as "" for abstract lines (shares, rates, counts of people, air readings) — a forced emoji looks worse than none. One emoji each, the same emoji works for both languages. NEVER use a number/keycap emoji (0-9, #) — numbers only ever come from the data.
- Avoid the ids in AVOID_IDS.

Return ONLY JSON:
{"opener_en":"...","opener_ko":"...","opener_emoji":"<one emoji or ''>","note":"one line: what the juxtaposition is","picks":[{"id":"<pool id>","label_en":"<optional reword or copy>","label_ko":"<korean label>","emoji":"<one emoji or ''>"}]}
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


def _valid_emoji(s):
    """Return a single tasteful emoji if `s` is one, else ''. The card design
    lets the selector tag lines with an emoji, but numbers must stay Python's
    alone: reject anything carrying a digit or a keycap (0-9, #, *) so a figure
    can never reach a post through an emoji. Also reject non-emoji text so a
    stray label word can't slip in."""
    if not s or not s.strip():
        return ''
    s = s.strip()
    if any(ch.isdigit() for ch in s):
        return ''
    cps = [ord(ch) for ch in s]
    if 0x20E3 in cps or ord('#') in cps or ord('*') in cps or len(cps) > 8:
        return ''

    def emoji_ish(o):
        return (0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or
                0x2B00 <= o <= 0x2BFF or 0x2190 <= o <= 0x21FF or
                0x1F1E6 <= o <= 0x1F1FF or 0x1F3FB <= o <= 0x1F3FF or
                o in (0x200D, 0xFE0F, 0x2122, 0x2139, 0x203C, 0x2049))

    def pictograph(o):
        return (0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or
                0x2B00 <= o <= 0x2BFF or 0x1F1E6 <= o <= 0x1F1FF)

    if not all(emoji_ish(o) for o in cps) or not any(pictograph(o) for o in cps):
        return ''
    return s


def _sortkey(value_en):
    """(unit_class, magnitude) for a formatted value, or None if unparseable.
    Lets compose() order a post's lines by size, but only among lines that share
    a unit (so a ₩ post sorts, a mixed count+% narrative post is left alone)."""
    s = value_en.strip()
    if s.startswith('₩'):
        num, mult = s[1:], 1.0
        for suf, m in (('tn', 1e12), ('bn', 1e9), ('m', 1e6)):
            if num.endswith(suf):
                num, mult = num[:-len(suf)], m
                break
        try:
            return ('won', float(num.replace(',', '')) * mult)
        except ValueError:
            return None
    if s.endswith('%'):
        try:
            return ('pct', float(s[:-1]))
        except ValueError:
            return None
    if 'µg' in s:
        try:
            return ('air', float(s.split()[0]))
        except ValueError:
            return None
    try:
        return ('num', float(s.replace(',', '')))
    except ValueError:
        return None


# --- label de-duplication --------------------------------------------------

# Words that must not be left stranded at the end of a trimmed English label.
_EN_DANGLERS = {'in', 'at', 'on', 'of', 'for', 'to', 'per', 'the', 'a', 'an',
                'from', 'by', 'with', 'and', 'who', 'that'}


def _common_run(seqs, from_end):
    """Length of the longest run of identical tokens shared by EVERY sequence,
    counted from the start or the end. Never consumes a whole sequence, so every
    label keeps at least one token."""
    n = 0
    while all(len(s) > n for s in seqs):
        pos = -1 - n if from_end else n
        if len({s[pos] for s in seqs}) != 1:
            break
        n += 1
    return n


def _opener_covers(tokens, opener):
    """True if the opener already says all of `tokens`. When it does, repeating
    them on the lines is pure redundancy and the run can go from the first line
    too; when it doesn't, the run stays on the first line so the framing is
    stated once rather than lost."""
    if not tokens:
        return False
    have = set(re.sub(r'[^\w\s]', ' ', opener.lower()).split())
    return all(re.sub(r'\W', '', t.lower()) in have for t in tokens)


def dedupe_labels(labels, opener, korean=False):
    """Trim wording that every label in a post repeats, so the card reads the way
    a Harper's Index does: the metric is named once, and each later line carries
    only what actually differs.

        Estimated crowd in Jamsil right now     Estimated crowd in Jamsil
        Estimated crowd in Hongdae right now -> In Hongdae
        Estimated crowd at the Yeouido riverbank right now
                                                At the Yeouido riverbank

    Both the leading and the trailing shared run are dropped from every line but
    the first. A run the OPENER already carries ("right now" under the opener
    "Seoul, right now") is dropped from the first line as well, since the reader
    has just read it. Nothing that appears nowhere else is ever discarded.

    English is head-initial, so the metric leads and the time frame trails;
    Korean is head-final, so the two swap. The rule is symmetric, so the same
    code handles both: `korean` only suppresses re-capitalisation.

    Returns the labels untouched whenever there is nothing safe to trim."""
    if len(labels) < 3:
        return labels
    toks = [l.split() for l in labels]
    shortest = min(len(t) for t in toks)
    n_pre = _common_run(toks, from_end=False)
    # Leave at least one token that belongs to the line itself.
    n_suf = min(_common_run(toks, from_end=True), shortest - n_pre - 1)
    n_suf = max(n_suf, 0)
    # A trim that strands a preposition ("Coffee shops in") is worse than the
    # repetition it removes, so drop that end rather than mangle the line.
    if n_suf and not korean:
        if any(t[-1 - n_suf].lower().strip(',') in _EN_DANGLERS for t in toks):
            n_suf = 0
    pre, suf = toks[0][:n_pre], (toks[0][len(toks[0]) - n_suf:] if n_suf else [])
    first_drops_pre = _opener_covers(pre, opener)
    first_drops_suf = _opener_covers(suf, opener)

    out = []
    for i, t in enumerate(toks):
        cut_pre = n_pre if (i or first_drops_pre) else 0
        cut_suf = n_suf if (i or first_drops_suf) else 0
        rest = t[cut_pre:len(t) - cut_suf]
        if not rest:                      # nothing left to say — keep the original
            out = list(labels)
            break
        s = ' '.join(rest)
        if cut_pre and not korean and s[:1].islower():
            s = s[0].upper() + s[1:]
        out.append(s)
    # Runs the whole post shares are gone; the first line may still echo the
    # opener on its own (nothing shared it, so nothing above caught it).
    out[0] = _drop_opener_echo(out[0], opener, korean)
    return out


def _drop_opener_echo(label, opener, korean):
    """Trim the framing off the one line that still carries it.

    When the selector has already written the later lines bare, there is no run
    shared by every line for dedupe_labels() to catch, and the first line keeps
    its full pool label: "Estimated crowd in Myeongdong right now" under the
    opener "Seoul, right now". Strip the longest run of framing words the opener
    already says, from the end in English and the start in Korean. Refuses any
    trim that would strand a preposition or empty the label."""
    t = label.split()
    n = 0
    while n < len(t) - 1 and _opener_covers([t[-1 - n] if not korean else t[n]], opener):
        n += 1
    if not n:
        return label
    rest = t[:len(t) - n] if not korean else t[n:]
    if not korean and rest[-1].lower().strip(',') in _EN_DANGLERS:
        return label
    return ' '.join(rest)


def compose(sel, pool):
    by_id = {f['id']: f for f in pool}
    picks = [p for p in sel.get('picks', []) if p.get('id') in by_id]
    if len(picks) < 3:
        raise RuntimeError(f'selector returned too few valid picks: {len(picks)}')
    # A spotlight card is one place read along a clock — now, then the usual for
    # this hour, then the hours ahead. Sorting that by size would scramble the
    # sequence into nonsense, so it keeps the harvester's order instead.
    if any(by_id[p['id']]['cat'] == 'spotlight' for p in picks):
        order = {f['id']: i for i, f in enumerate(pool)}
        picks = sorted(picks, key=lambda p: order.get(p['id'], 0))
    else:
        # Order the lines by value, largest first, but only when every line shares a
        # unit (an all-₩ or all-% post). Mixed-unit posts (e.g. a national post's two
        # population counts then a share %) keep the selector's narrative order.
        keys = [_sortkey(by_id[p['id']]['value_en']) for p in picks]
        if all(k is not None for k in keys) and len({k[0] for k in keys}) == 1:
            picks = [p for _, p in sorted(zip(keys, picks),
                                          key=lambda kp: kp[0][1], reverse=True)]
    lines, used, cats, estimated, forecast = [], [], set(), False, False
    for p in picks:
        f = by_id[p['id']]
        label_en = clean_label(p.get('label_en'), f['label_en'], f['value_en'])
        # A fact that ships its own Korean label keeps it: those labels carry
        # clock times, and a time is a number Python does not hand over.
        label_ko = (f['label_ko'] if f.get('label_ko')
                    else clean_label(p.get('label_ko'), f['label_en'], f['value_ko']))
        lines.append({'emoji': _valid_emoji(p.get('emoji')),
                      'label_en': label_en, 'label_ko': label_ko,
                      'value_en': f['value_en'], 'value_ko': f['value_ko']})
        used.append(f['id'])
        cats.add(f['cat'])
        estimated = estimated or f['estimated']
        forecast = forecast or f.get('forecast')

    opener_en = clean_opener(sel.get('opener_en'), 'Seoul by the numbers')
    opener_ko = clean_opener(sel.get('opener_ko'), '숫자로 보는 서울')
    opener_emoji = _valid_emoji(sel.get('opener_emoji'))

    # Say the shared part once: the selector is asked for bare labels, but it
    # often copies a pool label verbatim onto every line, so trim deterministically
    # rather than trust the prompt.
    for lang, ko in (('en', False), ('ko', True)):
        opener = opener_en if lang == 'en' else opener_ko
        trimmed = dedupe_labels([l[f'label_{lang}'] for l in lines], opener, korean=ko)
        for l, t in zip(lines, trimmed):
            l[f'label_{lang}'] = t

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
    # How the crowd figures are arrived at is a caveat on the numbers themselves,
    # not a credit, so it rides on the card beside them rather than in the source
    # reply. It carries no link, so nothing is lost by taking it off the reply.
    # A spotlight card's later lines are predictions, and saying so is the whole
    # reason it is not headed "today".
    if forecast:
        note_en = 'Hours ahead are forecasts; crowds are KT-estimated'
        note_ko = '이후 시간대는 예측치 · 인구는 KT 추정'
    else:
        note_en = 'Crowds are KT-estimated' if estimated else ''
        note_ko = '인구는 KT 추정' if estimated else ''

    cat_list = [by_id[p['id']]['cat'] for p in picks]
    primary = max(set(cat_list), key=cat_list.count)

    # Plaintext bodies (opener + lines + source), used as the card's alt text and
    # as the whole post if card rendering fails. Emoji sit ahead of the label, as
    # on the card; the card's "##" markdown token is card-only decoration.
    def _pl(emoji, label, value):
        return f'{emoji} {label}: {value}' if emoji else f'{label}: {value}'
    op_en = f'{opener_emoji} {opener_en}' if opener_emoji else opener_en
    op_ko = f'{opener_emoji} {opener_ko}' if opener_emoji else opener_ko
    en_body = op_en + ':\n' + '\n'.join(
        _pl(l['emoji'], l['label_en'], l['value_en']) for l in lines) + (
        f'\n{note_en}' if note_en else '') + '\n' + src_en
    ko_body = op_ko + ':\n' + '\n'.join(
        _pl(l['emoji'], l['label_ko'], l['value_ko']) for l in lines) + (
        f'\n{note_ko}' if note_ko else '') + '\n' + src_ko

    return {
        'opener': {'emoji': opener_emoji, 'en': opener_en, 'ko': opener_ko},
        'lines': lines, 'src_en': src_en, 'src_ko': src_ko,
        'note_en': note_en, 'note_ko': note_ko,
        'en_body': en_body, 'ko_body': ko_body,
        'used': used, 'cats': list(cats), 'primary': primary,
    }


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


# --- card rendering --------------------------------------------------------

def _card_payload(c, lang):
    """Pull the card's opener, lines and footnote for one language out of
    compose()'s output."""
    opener = {'emoji': c['opener']['emoji'], 'text': c['opener'][lang]}
    lines = [{'emoji': l['emoji'], 'label': l[f'label_{lang}'], 'value': l[f'value_{lang}']}
             for l in c['lines']]
    return opener, lines, c[f'note_{lang}']


def render_pair(c, out_dir):
    """Render the EN and KO cards into out_dir. Returns ((path,size),(path,size))."""
    en_op, en_lines, en_note = _card_payload(c, 'en')
    ko_op, ko_lines, ko_note = _card_payload(c, 'ko')
    en = render_card(en_op, en_lines, Path(out_dir) / 'card_en.png', footnote=en_note)
    ko = render_card(ko_op, ko_lines, Path(out_dir) / 'card_ko.png', korean=True,
                     footnote=ko_note)
    return en, ko


# --- main ------------------------------------------------------------------

def main():
    config = json.loads(CONFIG.read_text())
    api_key = config['api_key']
    kosis_key = config.get('kosis_key')
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    # Every SPOTLIGHT_EVERY-th post drills into one place instead of setting
    # places against each other, cycling through the curated spots. These are
    # interspersed with the usual index cards, not a replacement for them, and a
    # place that does not answer with enough lines simply falls back to one.
    post_n = int(state.get('post_n', 0)) + 1
    state['post_n'] = post_n
    want_spotlight = FORCE_SPOTLIGHT or post_n % SPOTLIGHT_EVERY == 0
    if want_spotlight:
        i = int(state.get('spotlight_i', 0))
        spot = CROWD_SPOTS[i % len(CROWD_SPOTS)]
        facts = spotlight_facts(api_key, spot)
        if facts:
            state['spotlight_i'] = (i + 1) % len(CROWD_SPOTS)
            print(f'Spotlight post #{post_n}: {spot[1]} ({len(facts)} lines, '
                  f'no selector call).')
            sel, pool = spotlight_sel(spot, facts), facts
        else:
            print(f'Spotlight on {spot[1]} returned too little; normal index instead.')
            want_spotlight = False

    if not want_spotlight:
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

    c = compose(sel, pool)
    used, primary = c['used'], c['primary']

    # Each card posts as an image with NO caption, so the card sits at the very
    # top of its post; the source line + hashtags follow as their own threaded
    # reply, which keeps data.seoul.go.kr a real clickable link. The full
    # plaintext body is the card's alt text, and the whole post if rendering fails.
    en_source = add_tags(client_utils.TextBuilder(), c['src_en'])
    ko_source = add_tags(client_utils.TextBuilder(), c['src_ko'])
    en_alt, ko_alt = c['en_body'], c['ko_body']

    print(f'\nNote: {sel.get("note", "")}')
    print(f'\nEN alt / fallback ({len(en_alt)} chars):\n{"-"*46}\n{en_alt}\n{"-"*46}')
    print(f'\nKO alt / fallback ({len(ko_alt)} chars):\n{"-"*46}\n{ko_alt}\n{"-"*46}')
    print(f'\nEN source post: {en_source.build_text()!r}\nKO source post: {ko_source.build_text()!r}')

    # The image caption is always short; this guard protects the plaintext
    # FALLBACK that posts if card rendering fails.
    if len(en_alt) > MAX_POST_CHARS or len(ko_alt) > MAX_POST_CHARS:
        sys.exit(f'Fallback text too long (EN {len(en_alt)}, KO {len(ko_alt)}; '
                 f'max {MAX_POST_CHARS}). Re-run to reselect.')

    # Render both cards; any failure drops us to a plaintext thread so a post
    # never fails to go out over a rendering hiccup.
    cards = None
    try:
        out_dir = Path.cwd() if DRY_RUN else tempfile.mkdtemp()
        (en_path, en_size), (ko_path, ko_size) = render_pair(c, out_dir)
        cards = {'en': (Path(en_path).read_bytes(), en_size),
                 'ko': (Path(ko_path).read_bytes(), ko_size)}
        print(f'\nRendered cards — EN {en_size}, KO {ko_size}.')
        if not DRY_RUN:
            import shutil
            shutil.rmtree(out_dir, ignore_errors=True)
    except CardRenderError as e:
        print(f'\nCard render failed ({e}); falling back to a plaintext thread.')

    if DRY_RUN:
        if cards:
            print(f'\n(dry run — wrote {out_dir}/card_en.png and card_ko.png, not posting)')
        else:
            print('\n(dry run — not posting)')
        return

    handle = config['handle']
    password = keychain_password(handle, KEYCHAIN_SERVICE)
    bsky = Client()
    bsky.login(handle, password)
    if cards:
        (en_bytes, en_size), (ko_bytes, ko_size) = cards['en'], cards['ko']
        en_ar = models.AppBskyEmbedDefs.AspectRatio(width=en_size[0], height=en_size[1])
        ko_ar = models.AppBskyEmbedDefs.AspectRatio(width=ko_size[0], height=ko_size[1])

        def _reply(parent_ref, root_ref):
            return models.AppBskyFeedPost.ReplyRef(parent=parent_ref, root=root_ref)

        # 4-post chain: EN card → EN source → KO card → KO source. Cards carry no
        # text so the image is first; each source reply carries the clickable
        # link + tags. Every reply's root stays the first (EN card) post.
        p1 = bsky.send_image(text='', image=en_bytes, image_alt=en_alt,
                             langs=['en'], image_aspect_ratio=en_ar)
        root_ref = models.create_strong_ref(p1)
        p2 = bsky.send_post(text=en_source, reply_to=_reply(root_ref, root_ref), langs=['en'])
        p2_ref = models.create_strong_ref(p2)
        p3 = bsky.send_image(text='', image=ko_bytes, image_alt=ko_alt,
                             reply_to=_reply(p2_ref, root_ref), langs=['ko'],
                             image_aspect_ratio=ko_ar)
        p3_ref = models.create_strong_ref(p3)
        bsky.send_post(text=ko_source, reply_to=_reply(p3_ref, root_ref), langs=['ko'])
        print('\nPosted (4-post thread: EN card, EN source, KO card, KO source).')
    else:
        en_full = add_tags(client_utils.TextBuilder(), c['en_body'])
        ko_full = add_tags(client_utils.TextBuilder(), c['ko_body'])
        root = bsky.send_post(text=en_full, langs=['en'])
        root_ref = models.create_strong_ref(root)
        reply_ref = models.AppBskyFeedPost.ReplyRef(parent=root_ref, root=root_ref)
        bsky.send_post(text=ko_full, reply_to=reply_ref, langs=['ko'])
        print('\nPosted (English + Korean thread, plaintext fallback).')

    recent_ids = (state.get('recent_ids', []) + used)[-RECENT_IDS_KEEP:]
    state['recent_ids'] = recent_ids
    state['last_cat'] = primary
    state['last_success_at'] = datetime.now(timezone.utc).isoformat()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
