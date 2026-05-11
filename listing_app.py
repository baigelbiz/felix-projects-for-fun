#!/usr/bin/env python3
"""
Shefa Listings — AI listing generator for the Israeli second-hand market.

Snap a photo → Claude analyzes the item → searches live market prices →
returns ready-to-post Hebrew listings for Yad2 (יד2) and Facebook Marketplace.

Endpoints:
  GET  /             Web UI  (upload form)
  POST /analyze      Generate listing from uploaded photo  → JSON
  POST /telegram     Telegram bot webhook
  GET  /set_webhook  Register Telegram webhook  (call once after deploy)
  GET  /health       Health check

Required env vars:  ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN
Optional env vars:  PORT (default 8080), WEBHOOK_BASE_URL (e.g. https://myapp.onrender.com)
"""

import base64
import json
import logging
import os
import time
from pathlib import Path

import anthropic
import requests
from flask import Flask, Response, jsonify, request

# ── env ──────────────────────────────────────────────────────────────────────
for _f in [Path(__file__).parent / '.env.listings', Path(__file__).parent / '.env']:
    if _f.exists():
        for _line in _f.read_text().splitlines():
            if '=' in _line and not _line.startswith('#'):
                k, v = _line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))

ANTHROPIC_API_KEY  = os.environ['ANTHROPIC_API_KEY']
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
WEBHOOK_BASE_URL   = os.environ.get('WEBHOOK_BASE_URL', '').rstrip('/')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

ai     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
app    = Flask(__name__)
TG_API = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}'

app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20 MB upload limit

# ── JSON schema for listing output ───────────────────────────────────────────
LISTING_SCHEMA = {
    "type": "object",
    "properties": {
        "item_name":  {"type": "string"},
        "condition":  {"type": "string"},
        "market": {
            "type": "object",
            "properties": {
                "price_min":         {"type": "number"},
                "price_max":         {"type": "number"},
                "recommended_price": {"type": "number"},
                "market_summary":    {"type": "string"},
            },
            "required": ["price_min", "price_max", "recommended_price", "market_summary"],
            "additionalProperties": False,
        },
        "yad2": {
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "description": {"type": "string"},
                "price":       {"type": "number"},
                "category":    {"type": "string"},
            },
            "required": ["title", "description", "price", "category"],
            "additionalProperties": False,
        },
        "facebook": {
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "description": {"type": "string"},
                "price":       {"type": "number"},
            },
            "required": ["title", "description", "price"],
            "additionalProperties": False,
        },
        "selling_tips": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["item_name", "condition", "market", "yad2", "facebook", "selling_tips"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
אתה מומחה למכירות בשוק יד שנייה בישראל עם גישה לחיפוש באינטרנט.
תפקידך:
1. לזהות את הפריט בתמונה ולהעריך את מצבו.
2. לחפש מחירים עדכניים לפריט דומה ביד2 ובפייסבוק מרקטפלייס ישראל.
3. לצור מודעת מכירה מקצועית ומושכת בעברית.

כללי מחיר: בסס את המחיר על נתוני שוק אמיתיים. המחיר המומלץ צריך להיות
אטרקטיבי אך לא נמוך מדי. לקטגוריה של יד2 השתמש בקטגוריות האמיתיות
(ריהוט, אלקטרוניקה, ביגוד, ספרים, צעצועים, ספורט, מוזיקה, כלי בית וכד').
"""

ANALYSIS_PROMPT = """\
שלב 1 — זהה את הפריט בתמונה: שם, מצב (חדש/כמו חדש/מצוין/טוב/תקין/ישן), חומר, מידות משוערות.

שלב 2 — חפש מחירים עדכניים של פריטים דומים ביד2 ובפייסבוק מרקטפלייס ישראל
(השתמש בכלי החיפוש).

שלב 3 — צור מודעת מכירה מלאה. החזר JSON לפי הסכימה המוגדרת.
market.market_summary: 2-3 משפטים על טווח המחירים שמצאת בשוק.
yad2.title: עד 60 תווים.
selling_tips: 2-3 טיפים פרקטיים למכירה מהירה.
"""


def analyze_image(image_bytes: bytes, mime_type: str = 'image/jpeg') -> dict:
    """
    Two-pass analysis:
      Pass 1 — Claude vision identifies the item.
      Pass 2 — Claude with web_search finds live market prices, then emits
               the final structured listing via json_schema output format.
    """
    b64 = base64.standard_b64encode(image_bytes).decode()

    # Pass 1: identify item with vision (quick, no tools needed)
    id_resp = ai.messages.create(
        model='claude-opus-4-7',
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": "מה הפריט בתמונה? תאר בקצרה בעברית: שם, מצב ומידות משוערות."},
            ],
        }],
    )
    item_desc = next(b.text for b in id_resp.content if b.type == "text").strip()
    log.info(f"Item identified: {item_desc[:80]}")

    # Pass 2: web-search pricing + generate full listing
    response = ai.messages.create(
        model='claude-opus-4-7',
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {
                    "type": "text",
                    "text": (
                        f"הפריט שזוהה: {item_desc}\n\n"
                        f"{ANALYSIS_PROMPT}"
                    ),
                },
            ],
        }],
        output_config={"format": {"type": "json_schema", "schema": LISTING_SCHEMA}},
    )

    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg_get(method: str, **params) -> dict:
    r = requests.get(f'{TG_API}/{method}', params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def tg_post(method: str, **payload) -> dict:
    r = requests.post(f'{TG_API}/{method}', json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def tg_send(chat_id: int, text: str) -> None:
    tg_post('sendMessage', chat_id=chat_id, text=text, parse_mode='HTML')


def tg_download_photo(file_id: str) -> tuple[bytes, str]:
    """Download the highest-res photo from Telegram."""
    file_info = tg_get('getFile', file_id=file_id)
    file_path = file_info['result']['file_path']
    r = requests.get(
        f'https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}',
        timeout=60,
    )
    r.raise_for_status()
    return r.content, 'image/jpeg'


def format_listing_telegram(data: dict) -> str:
    m = data['market']
    y = data['yad2']
    f = data['facebook']

    lines = [
        f"🏷 <b>{data['item_name']}</b>  |  {data['condition']}",
        "",
        f"📊 <b>מחקר שוק</b>",
        f"טווח מחירים: ₪{m['price_min']:,.0f} – ₪{m['price_max']:,.0f}",
        f"מחיר מומלץ: <b>₪{m['recommended_price']:,.0f}</b>",
        f"<i>{m['market_summary']}</i>",
        "",
        "📋 <b>יד2</b>",
        f"<b>כותרת:</b> {y['title']}",
        f"<b>תיאור:</b> {y['description']}",
        f"<b>מחיר:</b> ₪{y['price']:,.0f}",
        f"<b>קטגוריה:</b> {y['category']}",
        "",
        "💙 <b>Facebook Marketplace</b>",
        f"<b>כותרת:</b> {f['title']}",
        f"<b>תיאור:</b> {f['description']}",
        f"<b>מחיר:</b> ₪{f['price']:,.0f}",
    ]

    tips = data.get('selling_tips', [])
    if tips:
        lines += ["", "💡 <b>טיפים למכירה</b>"]
        lines += [f"• {t}" for t in tips]

    return "\n".join(lines)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    return 'ok'


@app.route('/')
def index():
    return Response(HTML_TEMPLATE, mimetype='text/html')


@app.route('/analyze', methods=['POST'])
def analyze():
    """Accept a photo upload, return listing JSON."""
    if 'photo' not in request.files:
        return jsonify(error='no photo'), 400

    file = request.files['photo']
    if not file.filename:
        return jsonify(error='empty filename'), 400

    image_bytes = file.read()
    mime_type   = file.content_type or 'image/jpeg'

    try:
        data = analyze_image(image_bytes, mime_type)
        return jsonify(data)
    except Exception as e:
        log.error(f'Analysis error: {e}', exc_info=True)
        return jsonify(error=str(e)), 500


@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    """Telegram bot webhook handler."""
    update  = request.get_json(silent=True) or {}
    message = update.get('message', {})
    chat_id = message.get('chat', {}).get('id')

    if not chat_id:
        return 'ok'

    text   = (message.get('text') or '').strip()
    photos = message.get('photo', [])

    if text in ('/start', '/help'):
        tg_send(chat_id,
            "שלום! 👋\n\n"
            "שלח לי <b>תמונה</b> של הפריט שברצונך למכור ואכין לך:\n"
            "• מחקר מחירים מהשוק 📊\n"
            "• מודעה מוכנה ליד2 📋\n"
            "• מודעה לפייסבוק מרקטפלייס 💙\n\n"
            "פשוט שלח תמונה ואני אטפל בשאר! 📸"
        )
    elif photos:
        tg_send(chat_id, "⏳ מנתח את הפריט ומחפש מחירים בשוק... (זה לוקח כ-30 שניות)")
        try:
            file_id             = photos[-1]['file_id']   # highest resolution
            image_bytes, mime   = tg_download_photo(file_id)
            data                = analyze_image(image_bytes, mime)
            tg_send(chat_id, format_listing_telegram(data))
        except Exception as e:
            log.error(f'Telegram analysis error: {e}', exc_info=True)
            tg_send(chat_id, "❌ אירעה שגיאה בניתוח התמונה. נסה שנית.")
    else:
        tg_send(chat_id, "📸 שלח לי תמונה של הפריט שברצונך למכור.")

    return 'ok'


@app.route('/set_webhook')
def set_webhook():
    """Register this server as the Telegram webhook (call once after deploy)."""
    if not TELEGRAM_BOT_TOKEN:
        return jsonify(error='TELEGRAM_BOT_TOKEN not set'), 500
    if not WEBHOOK_BASE_URL:
        return jsonify(error='WEBHOOK_BASE_URL not set'), 500

    url    = f'{WEBHOOK_BASE_URL}/telegram'
    result = tg_post('setWebhook', url=url)
    log.info(f'setWebhook → {result}')
    return jsonify(result)


# ── Web UI HTML ───────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shefa Listings – מחולל מודעות יד שנייה</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
    background: #0f0f13;
    color: #e8e8f0;
    min-height: 100vh;
    padding: 24px 16px;
  }
  header {
    text-align: center;
    margin-bottom: 32px;
  }
  header h1 { font-size: 2rem; background: linear-gradient(135deg,#a78bfa,#60a5fa); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
  header p  { color: #9ca3af; margin-top: 6px; font-size: 1rem; }

  .upload-area {
    border: 2px dashed #4b5563;
    border-radius: 16px;
    padding: 48px 24px;
    text-align: center;
    cursor: pointer;
    transition: border-color .2s, background .2s;
    max-width: 540px;
    margin: 0 auto 24px;
    position: relative;
  }
  .upload-area:hover, .upload-area.drag { border-color: #a78bfa; background: rgba(167,139,250,.06); }
  .upload-area input[type=file] { position:absolute; inset:0; opacity:0; cursor:pointer; width:100%; height:100%; }
  .upload-icon { font-size: 3rem; margin-bottom: 12px; }
  .upload-area p { color:#9ca3af; margin-top:8px; font-size:.9rem; }

  #preview-wrap { max-width:540px; margin:0 auto 24px; display:none; }
  #preview { width:100%; border-radius:12px; max-height:320px; object-fit:contain; background:#1a1a24; }

  .btn {
    display: block;
    width: 100%;
    max-width: 540px;
    margin: 0 auto 32px;
    padding: 14px;
    background: linear-gradient(135deg,#7c3aed,#2563eb);
    color: #fff;
    border: none;
    border-radius: 12px;
    font-size: 1.1rem;
    cursor: pointer;
    transition: opacity .2s;
  }
  .btn:disabled { opacity: .5; cursor: default; }
  .btn:hover:not(:disabled) { opacity: .9; }

  #loader { display:none; text-align:center; margin-bottom:24px; color:#a78bfa; }
  .spinner {
    width:40px; height:40px; border:4px solid #4b5563;
    border-top-color:#a78bfa; border-radius:50%;
    animation: spin 1s linear infinite; margin:0 auto 12px;
  }
  @keyframes spin { to { transform:rotate(360deg); } }

  #results { max-width:800px; margin:0 auto; }

  .card {
    background: #1a1a24;
    border: 1px solid #2d2d3d;
    border-radius: 16px;
    padding: 20px 24px;
    margin-bottom: 20px;
  }
  .card-header {
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 16px;
    font-size: 1.1rem; font-weight: 700;
  }

  .market-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin-bottom: 16px;
  }
  .price-box {
    background: #0f0f13;
    border-radius: 10px;
    padding: 12px;
    text-align: center;
  }
  .price-box .label { font-size:.75rem; color:#9ca3af; margin-bottom:4px; }
  .price-box .value { font-size:1.3rem; font-weight:700; color:#a78bfa; }
  .price-box.recommended .value { color:#34d399; font-size:1.5rem; }

  .field { margin-bottom: 14px; }
  .field label { display:block; font-size:.8rem; color:#9ca3af; margin-bottom:4px; }
  .field .value {
    background:#0f0f13; border-radius:8px; padding:10px 14px;
    font-size:.95rem; line-height:1.6;
    position: relative;
  }
  .copy-btn {
    position:absolute; top:8px; left:8px;
    background:#2d2d3d; border:none; color:#9ca3af;
    border-radius:6px; padding:3px 8px; font-size:.75rem;
    cursor:pointer; transition:background .15s;
  }
  .copy-btn:hover { background:#4b5563; color:#fff; }

  .tips-list { list-style:none; }
  .tips-list li { padding: 8px 0; border-bottom:1px solid #2d2d3d; color:#d1d5db; font-size:.95rem; }
  .tips-list li:last-child { border:none; }
  .tips-list li::before { content:"💡 "; }

  .tg-note {
    max-width:800px; margin:0 auto 32px;
    background:#1a1a24; border:1px solid #2d2d3d;
    border-radius:12px; padding:16px 20px;
    color:#9ca3af; font-size:.9rem; text-align:center;
  }
  .tg-note b { color:#60a5fa; }
</style>
</head>
<body>
<header>
  <h1>✨ Shefa Listings</h1>
  <p>צלם פריט → קבל מודעה מוכנה ליד2 ופייסבוק עם מחקר מחירים מהשוק</p>
</header>

<div class="tg-note">
  <b>בוט טלגרם זמין!</b> שלח תמונה ישירות לבוט וקבל מודעה תוך שניות.
  צור קשר עם מנהל המערכת לקבלת קישור.
</div>

<div class="upload-area" id="drop-zone">
  <input type="file" id="file-input" accept="image/*" capture="environment">
  <div class="upload-icon">📸</div>
  <h3>גרור תמונה לכאן או לחץ לבחירה</h3>
  <p>תמיכה ב-JPG, PNG, HEIC עד 20MB</p>
</div>

<div id="preview-wrap">
  <img id="preview" alt="תצוגה מקדימה">
</div>

<button class="btn" id="analyze-btn" disabled>נתח פריט וצור מודעה</button>

<div id="loader">
  <div class="spinner"></div>
  <p>מנתח פריט ומחפש מחירים בשוק... (כ-30 שניות)</p>
</div>

<div id="results"></div>

<script>
const dropZone   = document.getElementById('drop-zone');
const fileInput  = document.getElementById('file-input');
const previewWrap= document.getElementById('preview-wrap');
const preview    = document.getElementById('preview');
const analyzeBtn = document.getElementById('analyze-btn');
const loader     = document.getElementById('loader');
const results    = document.getElementById('results');

let selectedFile = null;

function setFile(file) {
  if (!file || !file.type.startsWith('image/')) return;
  selectedFile = file;
  const url = URL.createObjectURL(file);
  preview.src = url;
  previewWrap.style.display = 'block';
  analyzeBtn.disabled = false;
}

fileInput.addEventListener('change', () => setFile(fileInput.files[0]));

dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag'); });
dropZone.addEventListener('dragleave', ()=> dropZone.classList.remove('drag'));
dropZone.addEventListener('drop', e => {
  e.preventDefault(); dropZone.classList.remove('drag');
  setFile(e.dataTransfer.files[0]);
});

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = '✓ הועתק';
    setTimeout(() => btn.textContent = 'העתק', 1500);
  });
}

function priceILS(n) {
  return '₪' + Number(n).toLocaleString('he-IL');
}

function renderField(label, value, copyable) {
  const safe = value.toString().replace(/</g,'&lt;');
  const btn  = copyable
    ? `<button class="copy-btn" onclick="copyText(this.dataset.v, this)" data-v="${value.toString().replace(/"/g,'&quot;')}">העתק</button>`
    : '';
  return `<div class="field"><label>${label}</label><div class="value" style="padding-left:${copyable?'60px':'14px'}">${btn}${safe}</div></div>`;
}

analyzeBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  analyzeBtn.disabled = true;
  loader.style.display = 'block';
  results.innerHTML   = '';

  const fd = new FormData();
  fd.append('photo', selectedFile);

  try {
    const resp = await fetch('/analyze', { method:'POST', body:fd });
    if (!resp.ok) { const e = await resp.json(); throw new Error(e.error || resp.statusText); }
    const d = await resp.json();
    renderResults(d);
  } catch(err) {
    results.innerHTML = `<div class="card" style="border-color:#f87171;color:#f87171">שגיאה: ${err.message}</div>`;
  } finally {
    loader.style.display = 'none';
    analyzeBtn.disabled  = false;
  }
});

function renderResults(d) {
  const m = d.market;
  const y = d.yad2;
  const f = d.facebook;

  results.innerHTML = `
  <!-- Item header -->
  <div class="card">
    <div class="card-header">🏷 ${d.item_name} &nbsp;|&nbsp; <span style="color:#9ca3af;font-weight:400">${d.condition}</span></div>
  </div>

  <!-- Market research -->
  <div class="card">
    <div class="card-header">📊 מחקר שוק</div>
    <div class="market-grid">
      <div class="price-box"><div class="label">מחיר מינימום</div><div class="value">${priceILS(m.price_min)}</div></div>
      <div class="price-box recommended"><div class="label">מחיר מומלץ</div><div class="value">${priceILS(m.recommended_price)}</div></div>
      <div class="price-box"><div class="label">מחיר מקסימום</div><div class="value">${priceILS(m.price_max)}</div></div>
    </div>
    <div style="color:#9ca3af;font-size:.9rem;line-height:1.6">${m.market_summary}</div>
  </div>

  <!-- Yad2 -->
  <div class="card">
    <div class="card-header">📋 יד2</div>
    ${renderField('כותרת', y.title, true)}
    ${renderField('תיאור', y.description, true)}
    ${renderField('מחיר', priceILS(y.price), false)}
    ${renderField('קטגוריה', y.category, false)}
  </div>

  <!-- Facebook -->
  <div class="card">
    <div class="card-header">💙 Facebook Marketplace</div>
    ${renderField('כותרת', f.title, true)}
    ${renderField('תיאור', f.description, true)}
    ${renderField('מחיר', priceILS(f.price), false)}
  </div>

  <!-- Tips -->
  ${d.selling_tips && d.selling_tips.length ? `
  <div class="card">
    <div class="card-header">💡 טיפים למכירה מהירה</div>
    <ul class="tips-list">${d.selling_tips.map(t => `<li>${t}</li>`).join('')}</ul>
  </div>` : ''}
  `;

  results.scrollIntoView({ behavior:'smooth', block:'start' });
}
</script>
</body>
</html>
"""

# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    log.info(f'Listing app listening on port {port}')
    if WEBHOOK_BASE_URL:
        log.info(f'Telegram webhook will be at {WEBHOOK_BASE_URL}/telegram')
        log.info('Call GET /set_webhook once to register it with Telegram.')
    app.run(host='0.0.0.0', port=port, threaded=True)
