#!/usr/bin/env python3
"""
Probate Leads Automation

Reads weekly XLS attachments from jjsrvcs@yahoo.com via Gmail,
uses Gemini to analyse each lead (Creative Cache, Seller Findings, SMS Draft),
then creates fully-annotated leads in Close.io.

First-time Gmail setup:
  python3 probate_leads.py --auth

Process emails now:
  python3 probate_leads.py --run

Process a local XLS file (no Gmail needed):
  python3 probate_leads.py --file path/to/leads.xls

Run on schedule (every Friday 10:00 AM Israel time):
  python3 probate_leads.py --schedule
"""

import argparse
import base64
import io
import json
import logging
import os
import sys
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
import schedule
import xlrd
from google import genai
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

# ── env ───────────────────────────────────────────────────────────────────────
for _f in [Path(__file__).parent / '.env']:
    if _f.exists():
        for _line in _f.read_text().splitlines():
            if '=' in _line and not _line.startswith('#'):
                k, v = _line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))

CLOSE_API_KEY    = os.environ.get('CLOSE_API_KEY', '')
GEMINI_API_KEY   = os.environ.get('GEMINI_API_KEY', '')
TOKEN_FILE       = Path(os.environ.get('GMAIL_TOKEN_FILE', 'gmail_token.json'))
CREDENTIALS_FILE = Path(__file__).parent / 'gmail_credentials.json'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

GMAIL_SCOPES   = ['https://www.googleapis.com/auth/gmail.readonly']
REDIRECT_URI   = 'http://localhost:8090'
ISRAEL_TZ      = ZoneInfo('Asia/Jerusalem')
CLOSE_BASE     = 'https://api.close.com/api/v1'
SENDER_FILTER  = 'jjsrvcs@yahoo.com'
SUBJECT_FILTER = 'Probate Leads'

ai = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ── XLS column indices ────────────────────────────────────────────────────────
C = {
    'date_entered':   0,  'case_number':    1,  'date_filed':     2,
    'lawyer_name':    3,  'lawyer_address': 4,  'lawyer_city':    5,
    'lawyer_zip':     6,  'lawyer_phone':   7,  'lawyer_email':   8,
    'deceased_name':  9,  'petitioner':    10,  'exec_admin':    11,
    'authority':     12,  'bond':          13,  'date_passing':  14,
    'prop_address':  15,  'prop_city':     16,  'prop_zip':      17,
    'rental_income': 18,  'property_val':  19,  'encumbrances':  20,
    'line7_total':   21,
    'pet_address':   25,  'pet_city':      26,  'pet_state':     27,
    'pet_zip':       28,  'pet_relation':  29,  'pet_phone':     32,
}


# ── Gmail auth ────────────────────────────────────────────────────────────────

def gmail_auth():
    if not CREDENTIALS_FILE.exists():
        log.error(f'Missing {CREDENTIALS_FILE}')
        sys.exit(1)

    creds_data    = json.loads(CREDENTIALS_FILE.read_text())
    client_config = creds_data if 'installed' in creds_data else {'web': creds_data['web']}
    flow          = Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=REDIRECT_URI)
    auth_url, _   = flow.authorization_url(access_type='offline', prompt='consent')

    log.info('Opening browser for Gmail authorisation...')
    log.info(f'If browser does not open, visit:\n  {auth_url}')
    webbrowser.open(auth_url)

    auth_code = [None]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            auth_code[0] = parse_qs(urlparse(self.path).query).get('code', [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'<h2>Authorised! You can close this tab.</h2>')
        def log_message(self, *a): pass

    HTTPServer(('localhost', 8090), Handler).handle_request()
    if not auth_code[0]:
        log.error('No auth code received.')
        sys.exit(1)

    flow.fetch_token(code=auth_code[0])
    TOKEN_FILE.write_text(flow.credentials.to_json())
    log.info(f'Token saved to {TOKEN_FILE}')


def get_gmail_creds() -> Credentials:
    if not TOKEN_FILE.exists():
        log.error('Run: python3 probate_leads.py --auth')
        sys.exit(1)
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return creds


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def gmail_get(creds, path, **params):
    r = requests.get(f'https://gmail.googleapis.com/gmail/v1/{path}',
                     headers={'Authorization': f'Bearer {creds.token}'},
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_xls_attachments(creds, since_days=8):
    query = f'from:{SENDER_FILTER} subject:"{SUBJECT_FILTER}" has:attachment newer_than:{since_days}d'
    msgs  = gmail_get(creds, 'users/me/messages', q=query, maxResults=20).get('messages', [])
    log.info(f'Found {len(msgs)} matching email(s)')
    result = []
    for m in msgs:
        msg = gmail_get(creds, f'users/me/messages/{m["id"]}', format='full')
        result.extend(_extract_xls(creds, msg))
    return result


def _extract_xls(creds, msg):
    out = []
    def walk(parts):
        for p in parts:
            if p.get('filename', '').lower().endswith(('.xls', '.xlsx')):
                att_id = p.get('body', {}).get('attachmentId')
                if att_id:
                    data = gmail_get(creds, f'users/me/messages/{msg["id"]}/attachments/{att_id}')
                    out.append(base64.urlsafe_b64decode(data['data']))
                    log.info(f'Downloaded: {p["filename"]}')
            walk(p.get('parts', []))
    walk(msg.get('payload', {}).get('parts', []))
    return out


# ── XLS parser ────────────────────────────────────────────────────────────────

def parse_leads(xls_bytes: bytes) -> list[dict]:
    wb    = xlrd.open_workbook(file_contents=xls_bytes)
    sheet = wb.sheets()[0]

    header_row = next(
        (i for i in range(sheet.nrows)
         if any('Case Number' in str(v) for v in sheet.row_values(i))),
        None
    )
    if header_row is None:
        log.warning('Header row not found')
        return []

    leads = []
    for i in range(header_row + 1, sheet.nrows):
        row      = sheet.row_values(i)
        case_num = str(row[C['case_number']]).strip() if len(row) > C['case_number'] else ''
        if not case_num or case_num in ('', '0.0', '0'):
            continue

        def cell(key):
            idx = C.get(key, -1)
            if idx < 0 or idx >= len(row): return ''
            v = row[idx]
            if isinstance(v, float) and v == int(v): return str(int(v))
            return str(v).strip() if v else ''

        def money(key):
            idx = C.get(key, -1)
            if idx < 0 or idx >= len(row): return 0.0
            try:   return float(row[idx])
            except: return 0.0

        leads.append({
            'case_number':   case_num,
            'date_entered':  cell('date_entered'),
            'date_filed':    cell('date_filed'),
            'deceased_name': cell('deceased_name'),
            'petitioner':    cell('petitioner'),
            'exec_admin':    cell('exec_admin'),
            'authority':     cell('authority'),
            'date_passing':  cell('date_passing'),
            'property_val':  money('property_val'),
            'encumbrances':  money('encumbrances'),
            'line7_total':   money('line7_total'),
            'rental_income': money('rental_income'),
            'prop_address':  cell('prop_address'),
            'prop_city':     cell('prop_city'),
            'prop_zip':      cell('prop_zip'),
            'lawyer': {
                'name':    cell('lawyer_name'),
                'address': cell('lawyer_address'),
                'city':    cell('lawyer_city'),
                'zip':     cell('lawyer_zip'),
                'phone':   cell('lawyer_phone'),
                'email':   cell('lawyer_email'),
            },
            'petitioner_contact': {
                'name':     cell('petitioner'),
                'address':  cell('pet_address'),
                'city':     cell('pet_city'),
                'state':    cell('pet_state'),
                'zip':      cell('pet_zip'),
                'phone':    cell('pet_phone'),
                'relation': cell('pet_relation'),
            },
        })

    log.info(f'Parsed {len(leads)} lead(s)')
    return leads


# ── Gemini analysis ───────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """\
You are a real estate investment analyst specialising in probate and Subject To (SubTo) deals.

Here is a probate lead from court records:

DECEASED: {deceased_name}
CASE #: {case_number} | Filed: {date_filed} | Date of Passing: {date_passing}
PROPERTY: {prop_address}, {prop_city} {prop_zip}
Property Value: ${property_val:,.0f}
Encumbrances/Mortgage: ${encumbrances:,.0f}
Equity (Line 7 Total): ${line7_total:,.0f}
Rental Income: ${rental_income:,.0f}/yr
Authority Type: {authority}
Petitioner: {petitioner} ({pet_relation}) | Phone: {pet_phone}
Petitioner Address: {pet_address}, {pet_city} {pet_state} {pet_zip}
Executor/Admin: {exec_admin}
Attorney: {lawyer_name} | {lawyer_phone} | {lawyer_email}

Analyse this lead and respond ONLY with valid JSON in this exact structure (no markdown, no extra text):

{{
  "creative_cache": {{
    "lead_source": "Probate Court Records via J&J Services",
    "property": "{prop_address}, {prop_city} {prop_zip}",
    "seller": "{petitioner}",
    "situation": "one sentence",
    "estimated_value": "${property_val:,.0f}",
    "mortgage_info": "based on encumbrances data",
    "equity": "${equity:,.0f}",
    "motivation": "one sentence",
    "subject_to_fit": "Likely / Possible / Not enough info",
    "missing_info": "what we need to find out",
    "suggested_strategy": "SubTo / Cash Offer / Seller Finance / Needs Discovery",
    "next_action": "specific next step"
  }},
  "seller_findings": {{
    "source": "Probate Court Records",
    "key_findings": "2-3 sentences",
    "motivation": "one sentence",
    "timeline": "estimate based on probate timeline",
    "property_condition": "Unknown — needs inspection",
    "occupancy": "Unknown — needs verification",
    "pain_points": "likely pain points for this type of probate",
    "risks_red_flags": "any concerns from the data",
    "recommended_approach": "one sentence"
  }},
  "recommendation": {{
    "action": "Call immediately / Send SMS first / Research mortgage first / Skip trace / Add to nurture / Possible Subject To / Possible cash offer",
    "reason": "one sentence why"
  }},
  "sms_draft": "Hi [Seller], this is Felix. I came across the estate of [deceased] and wanted to reach out. Would you be open to a quick conversation about the property?"
}}
"""

def gemini_analyse(lead: dict) -> dict:
    if not ai:
        return {}
    equity = lead['property_val'] - lead['encumbrances']
    pc     = lead['petitioner_contact']
    lw     = lead['lawyer']
    prompt = ANALYSIS_PROMPT.format(
        deceased_name=lead['deceased_name'],
        case_number=lead['case_number'],
        date_filed=lead['date_filed'],
        date_passing=lead['date_passing'],
        prop_address=lead['prop_address'],
        prop_city=lead['prop_city'],
        prop_zip=lead['prop_zip'],
        property_val=lead['property_val'],
        encumbrances=lead['encumbrances'],
        line7_total=lead['line7_total'],
        rental_income=lead['rental_income'],
        authority=lead['authority'],
        petitioner=pc['name'],
        pet_relation=pc['relation'],
        pet_phone=pc['phone'],
        pet_address=pc['address'],
        pet_city=pc['city'],
        pet_state=pc['state'],
        pet_zip=pc['zip'],
        exec_admin=lead['exec_admin'],
        lawyer_name=lw['name'],
        lawyer_phone=lw['phone'],
        lawyer_email=lw['email'],
        equity=equity,
    )
    try:
        resp = ai.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config={'temperature': 0.2},
        )
        text = resp.text.strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'): text = text[4:]
            text = text.strip()
        return json.loads(text)
    except Exception as e:
        log.warning(f'Gemini analysis failed for {lead["case_number"]}: {e}')
        return {}


def build_close_note(lead: dict, analysis: dict) -> str:
    equity = lead['property_val'] - lead['encumbrances']
    lines  = [
        f'📁 CASE #{lead["case_number"]}  |  Filed: {lead["date_filed"]}  |  Passing: {lead["date_passing"]}',
        f'⚖️  Authority: {lead["authority"]}  |  Executor/Admin: {lead["exec_admin"]}',
        '',
    ]

    cc = analysis.get('creative_cache', {})
    if cc:
        lines += [
            '═══ CREATIVE CACHE — SUBJECT TO ANALYSIS ═══',
            f'Lead Source:        {cc.get("lead_source","")}',
            f'Property:           {cc.get("property","")}',
            f'Seller:             {cc.get("seller","")}',
            f'Situation:          {cc.get("situation","")}',
            f'Estimated Value:    {cc.get("estimated_value","")}',
            f'Mortgage / Loan:    {cc.get("mortgage_info","")}',
            f'Equity:             {cc.get("equity","")}',
            f'Motivation:         {cc.get("motivation","")}',
            f'Subject To Fit:     {cc.get("subject_to_fit","")}',
            f'Missing Info:       {cc.get("missing_info","")}',
            f'Suggested Strategy: {cc.get("suggested_strategy","")}',
            f'Next Action:        {cc.get("next_action","")}',
            '',
        ]

    sf = analysis.get('seller_findings', {})
    if sf:
        lines += [
            '═══ SELLER FINDINGS ═══',
            f'Source:              {sf.get("source","")}',
            f'Key Findings:        {sf.get("key_findings","")}',
            f'Motivation:          {sf.get("motivation","")}',
            f'Timeline:            {sf.get("timeline","")}',
            f'Property Condition:  {sf.get("property_condition","")}',
            f'Occupancy:           {sf.get("occupancy","")}',
            f'Seller Pain Points:  {sf.get("pain_points","")}',
            f'Risks / Red Flags:   {sf.get("risks_red_flags","")}',
            f'Recommended Approach:{sf.get("recommended_approach","")}',
            '',
        ]

    rec = analysis.get('recommendation', {})
    if rec:
        lines += [
            '═══ RECOMMENDATION ═══',
            f'Action: {rec.get("action","")}',
            f'Reason: {rec.get("reason","")}',
            '',
        ]

    sms = analysis.get('sms_draft', '')
    if sms:
        lines += [
            '═══ SMS DRAFT ═══',
            sms,
            '',
        ]

    lines += [
        '═══ PROPERTY & FINANCIALS ═══',
        f'Address:        {lead["prop_address"]}, {lead["prop_city"]} {lead["prop_zip"]}',
        f'Property Value: ${lead["property_val"]:,.0f}',
        f'Encumbrances:   ${lead["encumbrances"]:,.0f}',
        f'Equity:         ${equity:,.0f}',
    ]
    if lead['rental_income']:
        lines.append(f'Rental Income:  ${lead["rental_income"]:,.0f}/yr')

    lines += [
        '',
        '═══ ATTORNEY ═══',
        f'{lead["lawyer"]["name"]}',
        f'Phone: {lead["lawyer"]["phone"]}  |  Email: {lead["lawyer"]["email"]}',
        f'{lead["lawyer"]["address"]}, {lead["lawyer"]["city"]} {lead["lawyer"]["zip"]}',
    ]

    return '\n'.join(lines)


# ── Close.io helpers ──────────────────────────────────────────────────────────

def close_req(method, path, **kwargs):
    r = requests.request(method, f'{CLOSE_BASE}/{path}/',
                         auth=(CLOSE_API_KEY, ''), timeout=30, **kwargs)
    r.raise_for_status()
    return r.json()


def case_exists(case_number: str) -> bool:
    data = close_req('GET', 'lead', params={'query': f'"{case_number}"', '_fields': 'id,display_name'})
    return len(data.get('data', [])) > 0


def create_close_lead(lead: dict, analysis: dict) -> str:
    equity = lead['property_val'] - lead['encumbrances']
    pc     = lead['petitioner_contact']
    lw     = lead['lawyer']
    rec    = analysis.get('recommendation', {})

    contacts = []
    if pc['name']:
        c = {'name': pc['name'], 'title': pc['relation'] or 'Petitioner'}
        if pc['phone']:   c['phones']    = [{'phone': pc['phone'], 'type': 'office'}]
        if pc['address']: c['addresses'] = [{'address_1': pc['address'], 'city': pc['city'],
                                              'state': pc['state'], 'zipcode': pc['zip']}]
        contacts.append(c)

    if lw['name']:
        c = {'name': lw['name'], 'title': 'Attorney'}
        if lw['phone']:   c['phones']    = [{'phone': lw['phone'], 'type': 'office'}]
        if lw['email']:   c['emails']    = [{'email': lw['email'], 'type': 'office'}]
        if lw['address']: c['addresses'] = [{'address_1': lw['address'], 'city': lw['city'],
                                              'zipcode': lw['zip']}]
        contacts.append(c)

    note_text = build_close_note(lead, analysis)

    result = close_req('POST', 'lead', json={
        'name':        f'Estate of {lead["deceased_name"]}',
        'description': (
            f'Case #{lead["case_number"]} | '
            f'Property: ${lead["property_val"]:,.0f} | '
            f'Equity: ${equity:,.0f} | '
            f'{rec.get("action","")}'
        ),
        'contacts': contacts,
    })
    lead_id = result['id']

    # Add full analysis as a note
    close_req('POST', 'activity/note', json={
        'lead_id': lead_id,
        'note':    note_text,
    })

    return lead_id


# ── Main run ──────────────────────────────────────────────────────────────────

def process_leads(leads: list[dict]):
    if not CLOSE_API_KEY:
        log.error('CLOSE_API_KEY not set in .env')
        return
    created = skipped = errors = 0
    for lead in leads:
        try:
            if case_exists(lead['case_number']):
                log.info(f'Skip (exists): {lead["case_number"]} — {lead["deceased_name"]}')
                skipped += 1
                continue

            log.info(f'Analysing: {lead["case_number"]} — {lead["deceased_name"]}')
            analysis = gemini_analyse(lead)

            lead_id = create_close_lead(lead, analysis)
            rec     = analysis.get('recommendation', {})
            log.info(f'Created: {lead["deceased_name"]} → {lead_id} | {rec.get("action","")}')
            created += 1
            time.sleep(0.5)
        except Exception as e:
            log.error(f'Error on {lead["case_number"]}: {e}')
            errors += 1

    log.info(f'Done — {created} created, {skipped} skipped, {errors} errors')


def run_from_gmail():
    log.info('=== Probate Leads: Gmail run ===')
    creds = get_gmail_creds()
    attachments = fetch_xls_attachments(creds)
    if not attachments:
        log.info('No new attachments found.')
        return
    for xls in attachments:
        process_leads(parse_leads(xls))


def run_from_file(path: str):
    log.info(f'=== Probate Leads: File run — {path} ===')
    xls_bytes = Path(path).read_bytes()
    process_leads(parse_leads(xls_bytes))


def run_scheduled():
    log.info('Scheduler active — every Friday 10:00 AM Israel time')
    schedule.every().friday.at('10:00').do(run_from_gmail)
    while True:
        schedule.run_pending()
        time.sleep(60)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Probate Leads → Close.io')
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--auth',     action='store_true', help='One-time Gmail OAuth')
    group.add_argument('--run',      action='store_true', help='Pull from Gmail now')
    group.add_argument('--file',     metavar='XLS',       help='Process a local XLS file')
    group.add_argument('--schedule', action='store_true', help='Scheduler mode (Fri 10am IST)')
    args = parser.parse_args()

    if args.auth:
        gmail_auth()
    elif args.run:
        run_from_gmail()
    elif args.file:
        run_from_file(args.file)
    elif args.schedule:
        run_scheduled()
