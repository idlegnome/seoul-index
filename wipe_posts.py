#!/usr/bin/env python3
"""
Delete EVERY post on @seoul-index.bsky.social, after archiving them.

Run by hand, never from launchd. This is irreversible: a deleted record cannot
be restored from the archive, only re-posted as a new post with a new URI.

  python3 wipe_posts.py --list    # show what would go, touch nothing
  python3 wipe_posts.py --wipe    # archive, confirm, then delete

--wipe requires typing DELETE at the prompt. Anything else aborts.

The archive (seoul_index_posts_archive.json, gitignored) holds each post's
rkey, timestamp, text and image alt text, so the wording survives even though
the posts do not. Images themselves are NOT archived; the cards are
reproducible from the code, and alt text carries the full body.

Records are listed straight from the PDS rather than from a feed view, because
a feed can filter replies out and the source replies are half this account.
"""

import json
import subprocess
import sys
from pathlib import Path

from atproto import Client

from seoul_index_post import CONFIG, KEYCHAIN_SERVICE, keychain_password

HERE = Path(__file__).parent
ARCHIVE = HERE / 'seoul_index_posts_archive.json'
PLC = 'https://plc.directory'


def _curl(url):
    r = subprocess.run(['curl', '-sS', url], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f'curl failed: {r.stderr.strip()}')
    return json.loads(r.stdout)


def all_records(did):
    """Every app.bsky.feed.post record in the repo, oldest last."""
    pds = next(s['serviceEndpoint'] for s in _curl(f'{PLC}/{did}')['service']
               if s['type'] == 'AtprotoPersonalDataServer')
    out, cursor = [], None
    while True:
        url = (f'{pds}/xrpc/com.atproto.repo.listRecords?repo={did}'
               f'&collection=app.bsky.feed.post&limit=100'
               + (f'&cursor={cursor}' if cursor else ''))
        page = _curl(url)
        out.extend(page.get('records', []))
        cursor = page.get('cursor')
        if not cursor or not page.get('records'):
            return pds, out


def summarise(recs):
    for r in sorted(recs, key=lambda r: r['value']['createdAt']):
        v = r['value']
        body = (v.get('text') or '').replace('\n', ' / ')[:60]
        kind = 'image' if 'images' in (v.get('embed') or {}) else 'text '
        print(f"  {v['createdAt'][:19]}  {kind}  {r['uri'].split('/')[-1]}  {body}")


def main():
    handle = json.loads(CONFIG.read_text())['handle']
    client = Client()
    profile = client.login(handle, keychain_password(handle, KEYCHAIN_SERVICE))
    pds, recs = all_records(profile.did)
    print(f'{handle} ({profile.did})\nPDS: {pds}\n{len(recs)} post records:\n')
    summarise(recs)

    if '--wipe' not in sys.argv:
        print('\n(--list: nothing deleted)')
        return

    ARCHIVE.write_text(json.dumps(
        [{'rkey': r['uri'].split('/')[-1], 'uri': r['uri'], 'cid': r['cid'],
          'createdAt': r['value']['createdAt'], 'text': r['value'].get('text', ''),
          'alt': [i.get('alt') for i in (r['value'].get('embed') or {}).get('images', [])]}
         for r in recs], ensure_ascii=False, indent=2))
    print(f'\nArchived {len(recs)} posts to {ARCHIVE}')

    print(f'\nAbout to permanently delete all {len(recs)} posts. This cannot be undone.')
    if input('Type DELETE to proceed: ').strip() != 'DELETE':
        sys.exit('Aborted; nothing deleted.')

    failed = []
    for i, r in enumerate(recs, 1):
        ok = client.delete_post(r['uri'])
        print(f'  [{i}/{len(recs)}] {r["uri"].split("/")[-1]} {"deleted" if ok else "FAILED"}')
        if not ok:
            failed.append(r['uri'])

    _, left = all_records(profile.did)
    print(f'\n{len(recs) - len(failed)} deleted, {len(failed)} failed, '
          f'{len(left)} records remaining.')
    if left or failed:
        sys.exit('Wipe incomplete - see above.')
    print('Account is empty. Next: python3 seoul_index_methodology.py --pin')


if __name__ == '__main__':
    main()
