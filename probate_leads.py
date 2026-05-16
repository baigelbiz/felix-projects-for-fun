#!/usr/bin/env python3
"""
Probate Leads Automation

Reads weekly XLS attachments from jjsrvcs@yahoo.com via Gmail,
parses each lead row, and creates leads in Close.io.

First-time setup:
  python3 probate_leads.py --auth     # opens browser to authorise Gmail

Trigger manually:
  python3 probate_leads.py --run

Run on schedule (every Friday 10:00 AM Israel time — keep terminal open):
  python3 probate_leads.py --schedule

Required env vars (in .env):
  CLOSE_API_KEY     — Close.io API key
  GMAIL_TOKEN_FILE  — path to saved token (default: gmail_token.json)
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

# ── XLS column indices (from row 50 header) ───────────────────────────────────
C = {
    'date_entered':   0,  'case_number':    1,  'date_filed':     2,
    'lawyer_name':    3,  'lawyer_address': 4,  'lawyer_city':    5,
    'lawyer_zip':     6,  'lawyer_phone':   7,  'lawyer_email':   8,
    'deceased_name':  9,  'petitioner':    10,  'exec_admin':    11,
    'authority':     12,  'bond':          13,  'date_passing':  14,
    'prop_address':  15,  'prop_city':     16,  'prop_zip':      17,
    'rental_income': 18,  'property_val':  19,  'encumbrances':  20,
    'line7_total':   21,  'ben1_name':     22,
    'pet_address':   25,  'pet_city':      26,  'pet_state':     27,
    'pet_zip':       28,  'pet_relation':  29,  'pet_phone':     32,
}


# ── Gmail auth ────────────────────────────────────────────────────────────────

def gmail_auth():
    """One-time OAuth flow. Saves token to TOKEN_FILE."""
    if not CREDENTIALS_FILE.exists():
        log.error(f'Missing {CREDENTIALS_FILE}. Place gmail_credentials.json next to this script.')
        sys.exit(1)

    creds_data = json.loads(CREDENTIALS_FILE.read_text())
    # Support both 'web' and 'installed' credential types
    client_config = creds_data if 'installed' in creds_data else {'web': creds_data['web']}

    flow = Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=REDIRECT_URI)
    auth_url, _ = flow.authorization_url(access_type='offline', prompt='consent')

    log.info('Opening browser for Gmail authorisation...')
    log.info(f'If browser does not open, visit:\n  {auth_url}')
    webbrowser.open(auth_url)

    # Capture the redirect with a tiny HTTP server
    auth_code = [None]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            auth_code[0] = params.get('code', [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'<h2>Authorised! You can close this tab.</h2>')

        def log_message(self, *a):
            pass

    server = HTTPServer(('localhost', 8090), Handler)
    server.handle_request()

    if not auth_code[0]:
        log.error('No auth code received.')
        sys.exit(1)

    flow.fetch_token(code=auth_code[0])
    creds = flow.credentials
    TOKEN_FILE.write_text(creds.to_json())
    log.info(f'Token saved to {TOKEN_FILE}')


def get_gmail_creds() -> Credentials:
    if not TOKEN_FILE.exists():
        log.error('No Gmail token found. Run: python3 probate_leads.py --auth')
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return creds


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def gmail_get(creds: Credentials, path: str, **params) -> dict:
    headers = {'Authorization': f'Bearer {creds.token}'}
    r = requests.get(f'https://gmail.googleapis.com/gmail/v1/{path}',
                     headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_xls_attachments(creds: Credentials, since_days: int = 8) -> list[bytes]:
    """Return list of raw XLS bytes from matching emails."""
    query = f'from:{SENDER_FILTER} subject:"{SUBJECT_FILTER}" has:attachment newer_than:{since_days}d'
    data  = gmail_get(creds, 'users/me/messages', q=query, maxResults=20)
    msgs  = data.get('messages', [])
    log.info(f'Found {len(msgs)} matching email(s)')

    attachments = []
    for msg_ref in msgs:
        msg = gmail_get(creds, f'users/me/messages/{msg_ref["id"]}', format='full')
        attachments.extend(_extract_xls(creds, msg))
    return attachments


def _extract_xls(creds: Credentials, msg: dict) -> list[bytes]:
    results = []
    parts   = msg.get('payload', {}).get('parts', [])

    def walk(parts):
        for part in parts:
            fname = part.get('filename', '')
            if fname.lower().endswith(('.xls', '.xlsx')):
                att_id = part.get('body', {}).get('attachmentId')
                if att_id:
                    data = gmail_get(creds, f'users/me/messages/{msg["id"]}/attachments/{att_id}')
                    raw  = base64.urlsafe_b64decode(data['data'])
                    results.append(raw)
                    log.info(f'Downloaded attachment: {fname} ({len(raw)} bytes)')
            walk(part.get('parts', []))

    walk(parts)
    return results


# ── XLS parser ────────────────────────────────────────────────────────────────

def parse_leads(xls_bytes: bytes) -> list[dict]:
    wb    = xlrd.open_workbook(file_contents=xls_bytes)
    sheet = wb.sheets()[0]

    # Find the header row (contains 'Case Number')
    header_row = None
    for i in range(sheet.nrows):
        row = sheet.row_values(i)
        if any('Case Number' in str(v) for v in row):
            header_row = i
            break

    if header_row is None:
        log.warning('Could not find header row in XLS')
        return []

    leads = []
    for i in range(header_row + 1, sheet.nrows):
        row = sheet.row_values(i)
        case_num = str(row[C['case_number']]).strip()
        if not case_num or case_num == '0.0':
            continue

        def cell(key):
            v = row[C[key]] if C[key] < len(row) else ''
            if isinstance(v, float) and v == int(v):
                return str(int(v))
            return str(v).strip() if v else ''

        def money(key):
            try:
                return float(row[C[key]]) if C[key] < len(row) else 0.0
            except (ValueError, TypeError):
                return 0.0

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

    log.info(f'Parsed {len(leads)} lead(s) from XLS')
    return leads


# ── Close.io helpers ──────────────────────────────────────────────────────────

def close_req(method: str, path: str, **kwargs) -> dict:
    r = requests.request(
        method, f'{CLOSE_BASE}/{path}/',
        auth=(CLOSE_API_KEY, ''),
        timeout=30, **kwargs
    )
    r.raise_for_status()
    return r.json()


def case_exists(case_number: str) -> bool:
    """Return True if a lead with this case number already exists in Close.io."""
    data = close_req('GET', 'lead', params={'query': f'"{case_number}"', '_fields': 'id,display_name'})
    return len(data.get('data', [])) > 0


def create_close_lead(lead: dict) -> str:
    """Create a lead in Close.io and return its ID."""
    deceased  = lead['deceased_name']
    case      = lead['case_number']
    prop_val  = lead['property_val']
    encumb    = lead['encumbrances']
    equity    = prop_val - encumb

    description = (
        f"Case #: {case}\n"
        f"Filed: {lead['date_filed']}  |  Date of Passing: {lead['date_passing']}\n"
        f"Authority: {lead['authority']}  |  Executor/Admin: {lead['exec_admin']}\n\n"
        f"Property: {lead['prop_address']}, {lead['prop_city']} {lead['prop_zip']}\n"
        f"Property Value: ${prop_val:,.0f}\n"
        f"Encumbrances:   ${encumb:,.0f}\n"
        f"Equity:         ${equity:,.0f}\n"
    )
    if lead['rental_income']:
        description += f"Rental Income:  ${lead['rental_income']:,.0f}/yr\n"

    contacts = []
    pc = lead['petitioner_contact']
    if pc['name']:
        contact = {
            'name':  pc['name'],
            'title': pc['relation'] or 'Petitioner',
        }
        if pc['phone']:
            contact['phones'] = [{'phone': pc['phone'], 'type': 'office'}]
        if pc['address']:
            contact['addresses'] = [{'address_1': pc['address'], 'city': pc['city'],
                                      'state': pc['state'], 'zipcode': pc['zip']}]
        contacts.append(contact)

    lw = lead['lawyer']
    if lw['name']:
        contact = {
            'name':  lw['name'],
            'title': 'Attorney',
        }
        if lw['phone']:
            contact['phones'] = [{'phone': lw['phone'], 'type': 'office'}]
        if lw['email']:
            contact['emails'] = [{'email': lw['email'], 'type': 'office'}]
        if lw['address']:
            contact['addresses'] = [{'address_1': lw['address'], 'city': lw['city'],
                                      'zipcode': lw['zip']}]
        contacts.append(contact)

    payload = {
        'name':        f'Estate of {deceased}',
        'description': description,
        'contacts':    contacts,
    }

    result = close_req('POST', 'lead', json=payload)
    return result['id']


# ── Main run ──────────────────────────────────────────────────────────────────

def run():
    log.info('=== Probate Leads run started ===')

    if not CLOSE_API_KEY:
        log.error('CLOSE_API_KEY not set in .env')
        return

    creds       = get_gmail_creds()
    attachments = fetch_xls_attachments(creds)

    if not attachments:
        log.info('No new attachments found.')
        return

    created = skipped = errors = 0
    for xls_bytes in attachments:
        leads = parse_leads(xls_bytes)
        for lead in leads:
            try:
                if case_exists(lead['case_number']):
                    log.info(f"Skip (exists): {lead['case_number']} — {lead['deceased_name']}")
                    skipped += 1
                    continue
                lead_id = create_close_lead(lead)
                log.info(f"Created: {lead['case_number']} — {lead['deceased_name']} → {lead_id}")
                created += 1
                time.sleep(0.3)  # stay under Close.io rate limit
            except Exception as e:
                log.error(f"Error on {lead['case_number']}: {e}")
                errors += 1

    log.info(f'=== Done: {created} created, {skipped} skipped, {errors} errors ===')


def run_scheduled():
    log.info('Scheduler started — will run every Friday at 10:00 AM Israel time')

    def job():
        now = datetime.now(ISRAEL_TZ)
        if now.weekday() == 4:  # Friday
            run()

    schedule.every().friday.at('10:00').do(run)
    while True:
        schedule.run_pending()
        time.sleep(60)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Probate Leads Automation')
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--auth',     action='store_true', help='One-time Gmail OAuth setup')
    group.add_argument('--run',      action='store_true', help='Process emails now')
    group.add_argument('--schedule', action='store_true', help='Run on schedule (Fri 10am Israel)')
    args = parser.parse_args()

    if args.auth:
        gmail_auth()
    elif args.run:
        run()
    elif args.schedule:
        run_scheduled()
