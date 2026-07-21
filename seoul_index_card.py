#!/usr/bin/env python3
"""
Card renderer for Seoul Index (@seoul-index.bsky.social).

Renders one post's index as a monospace "markdown on cream" PNG card, matching
the account avatar (cream #f5f0e6, red #d70000). Headless Google Chrome does the
type/emoji/Hangul layout (so color emoji and Korean Just Work via system fonts);
Pillow crops the result to the content.

Design is fixed by seoul_index_post.compose(): a bold header (## + optional
opener emoji + title), then one row per line (optional emoji + label, a red
dotted leader, a bold right-aligned value), then an optional muted footnote for
a caveat on the numbers ("Crowds are KT-estimated"). Source + hashtags are NOT
on the card — the poster keeps those as real, clickable text under the image,
which a rendered PNG cannot be.

The card is rendered on a magenta sentinel background and cropped to content, so
3-line, 4-line, and wrapped-long-label posts all come out tight with no guessed
height. Corners are square on purpose: Bluesky rounds image corners itself.

Public API:
    render_card(opener, lines, out_path, korean=False, footnote="") -> out_path
        opener:   {"emoji": "🧾" or "", "text": "Spent last quarter in Seoul"}
        lines:    [{"emoji": "☕" or "", "label": "Coffee shops",
                    "value": "₩651.4bn"}, ...]
        footnote: "Crowds are KT-estimated" or "" for none

Raises CardRenderError on any failure so the poster can fall back to plaintext.
"""

import html
import subprocess
import tempfile
from pathlib import Path

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
SENTINEL = 'FF00FF'          # page background; cropped away. Never appears in art.
SENTINEL_RGB = (255, 0, 255)
CARD_WIDTH = 600             # CSS px; device-scale 2 renders at 1200 px.
RENDER_HEIGHT = 1000         # generous CSS height; cropped to content after.
CREAM = '#f5f0e6'
RED = '#d70000'
INK = '#20242c'
BULLET = '#b0a487'
# Footnote ink: warm and quiet, but still 5.35:1 on the cream, so the caveat
# stays legible at 13px. (BULLET is only 2.17:1 — decoration, never text.)
MUTED = '#6b6152'

# Menlo covers latin + digits + the ₩ sign; Apple SD Gothic Neo covers Hangul;
# Apple Color Emoji is picked up automatically for emoji. Same stack both langs
# so the EN and KO siblings share one feel.
FONT_STACK = "Menlo, 'Apple SD Gothic Neo', monospace"


class CardRenderError(RuntimeError):
    """Rendering failed — caller should fall back to a plaintext post."""


def _esc(s):
    return html.escape(s or '', quote=True)


def _row_html(line):
    emoji = line.get('emoji') or ''
    lead = f'{_esc(emoji)} ' if emoji else ''
    return (
        '<div class="r">'
        f'<span class="lab">{lead}{_esc(line["label"])}</span>'
        '<span class="led"></span>'
        f'<span class="val">{_esc(line["value"])}</span>'
        '</div>'
    )


def _build_html(opener, lines, footnote=''):
    op_emoji = opener.get('emoji') or ''
    op_lead = f'{_esc(op_emoji)} ' if op_emoji else ''
    rows = ''.join(_row_html(l) for l in lines)
    foot = f'<div class="fn">{_esc(footnote)}</div>' if footnote else ''
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;background:#{SENTINEL}}}
.card{{width:{CARD_WIDTH}px;box-sizing:border-box;background:{CREAM};color:{INK};
  border-top:4px solid {RED};padding:28px 30px;font-family:{FONT_STACK}}}
.h{{font-size:17px;font-weight:700;margin-bottom:22px;line-height:1.35}}
.h .md{{color:{RED}}}
.r{{display:flex;align-items:flex-end;margin:13px 0;font-size:16px}}
.r .lab{{line-height:1;min-width:0;overflow-wrap:anywhere}}
.r .led{{flex:1 0 34px;border-bottom:2px dotted {RED};margin:0 9px}}
.r .val{{font-weight:700;line-height:1;white-space:nowrap}}
.fn{{margin-top:20px;font-size:13px;line-height:1.4;color:{MUTED}}}
</style></head><body>
<div class="card">
<div class="h"><span class="md">##</span> {op_lead}{_esc(opener['text'])}</div>
{rows}
{foot}
</div></body></html>"""


def _crop_to_content(raw_path, out_path):
    try:
        from PIL import Image, ImageChops
    except ImportError as e:
        raise CardRenderError(f'Pillow not available: {e}')
    with Image.open(raw_path) as im:
        im = im.convert('RGB')
        bg = Image.new('RGB', im.size, SENTINEL_RGB)
        bbox = ImageChops.difference(im, bg).getbbox()
        if not bbox:
            raise CardRenderError('rendered image was entirely background')
        cropped = im.crop(bbox)
        cropped.save(out_path)
        size = cropped.size
    return out_path, size


def _shoot(doc, out_path):
    """Render an HTML doc to a content-cropped PNG. Returns (out_path, (w, h))."""
    if not Path(CHROME).exists():
        raise CardRenderError(f'Chrome not found at {CHROME}')
    out_path = str(out_path)
    with tempfile.TemporaryDirectory() as td:
        html_path = Path(td) / 'card.html'
        raw_png = Path(td) / 'raw.png'
        html_path.write_text(doc, encoding='utf-8')
        cmd = [
            CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars',
            '--force-device-scale-factor=2',
            f'--window-size={CARD_WIDTH},{RENDER_HEIGHT}',
            f'--default-background-color={SENTINEL}FF',
            f'--screenshot={raw_png}', f'file://{html_path}',
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if not raw_png.exists():
            raise CardRenderError(
                f'Chrome produced no image (exit {r.returncode}): '
                f'{(r.stderr or r.stdout or "").strip()[:200]}')
        _, size = _crop_to_content(raw_png, out_path)
    return out_path, size


def render_card(opener, lines, out_path, korean=False, footnote=''):
    """Render one index card (label→leader→value rows, optional footnote).
    Returns (path, (w, h))."""
    if not lines:
        raise CardRenderError('no lines to render')
    return _shoot(_build_html(opener, lines, footnote), out_path)


# Source domains get bolded wherever they appear in prose body text.
PROSE_BOLD_TERMS = ('data.seoul.go.kr', 'kosis.kr')


def _prose_paragraph(p):
    s = _esc(p)
    for term in PROSE_BOLD_TERMS:
        s = s.replace(term, f'<b>{term}</b>')
    return f'<p>{s}</p>'


def _build_prose_html(heading, paragraphs, emoji=''):
    lead = f'{_esc(emoji)} ' if emoji else ''
    body = ''.join(_prose_paragraph(p) for p in paragraphs)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;background:#{SENTINEL}}}
.card{{width:{CARD_WIDTH}px;box-sizing:border-box;background:{CREAM};color:{INK};
  border-top:4px solid {RED};padding:28px 30px;font-family:{FONT_STACK}}}
.h{{font-size:17px;font-weight:700;margin-bottom:18px;line-height:1.35}}
.h .md{{color:{RED}}}
.body{{font-size:15px;line-height:1.65}}
.body p{{margin:0 0 12px}}
.body p:last-child{{margin-bottom:0}}
</style></head><body>
<div class="card">
<div class="h"><span class="md">##</span> {lead}{_esc(heading)}</div>
<div class="body">{body}</div>
</div></body></html>"""


def render_prose_card(heading, paragraphs, out_path, korean=False, emoji=''):
    """Render one prose card (heading + wrapped body paragraphs) in the same
    cream/red identity as the index cards. `paragraphs` is a list of strings.
    Returns (path, (w, h))."""
    if not paragraphs:
        raise CardRenderError('no body text to render')
    return _shoot(_build_prose_html(heading, paragraphs, emoji), out_path)


if __name__ == '__main__':
    # Manual smoke test: two posts, short and long-label.
    en = render_card(
        {'emoji': '🧾', 'text': 'Spent last quarter in Seoul'},
        [{'emoji': '☕', 'label': 'Coffee shops', 'value': '₩651.4bn'},
         {'emoji': '📚', 'label': 'Bookshops', 'value': '₩77.8bn'},
         {'emoji': '🍗', 'label': 'Fried-chicken shops', 'value': '₩77.7bn'},
         {'emoji': '🐾', 'label': 'Pet shops', 'value': '₩7.9bn'}],
        'card_en.png')
    national = render_card(
        {'emoji': '🇰🇷', 'text': 'Seoul and the nation'},
        [{'emoji': '', 'label': 'People who live in South Korea', 'value': '51,117,378'},
         {'emoji': '', 'label': 'People who live in Seoul', 'value': '9,299,548'},
         {'emoji': '', 'label': 'Share of all South Koreans who live in Seoul', 'value': '18.2%'}],
        'card_national.png')
    print('wrote card_en.png, card_national.png:', en, national)
