#!/usr/bin/env python3
"""
Push staged leads to Close.io.
Run from your Mac: python3 push_to_close.py
"""
import json, os, sys, time
from pathlib import Path

import requests

for line in open(Path(__file__).parent / '.env'):
    if '=' in line and not line.startswith('#'):
        k, v = line.strip().split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

CLOSE_API_KEY = os.environ.get('CLOSE_API_KEY', '')
CLOSE_BASE    = 'https://api.close.com/api/v1'

if not CLOSE_API_KEY:
    print('ERROR: CLOSE_API_KEY not set in .env'); sys.exit(1)

staged_file = Path(__file__).parent / 'staged_leads.json'
if not staged_file.exists():
    print('ERROR: staged_leads.json not found'); sys.exit(1)

staged = json.loads(staged_file.read_text())

def close_req(method, path, **kwargs):
    r = requests.request(method, f'{CLOSE_BASE}/{path}/',
                         auth=(CLOSE_API_KEY, ''), timeout=30, **kwargs)
    r.raise_for_status()
    return r.json()

def case_exists(case_number):
    data = close_req('GET', 'lead', params={'query': f'"{case_number}"', '_fields': 'id'})
    return len(data.get('data', [])) > 0

created = skipped = errors = 0
for item in staged:
    lead     = item['lead']
    analysis = item['analysis']
    note     = item['note']
    case     = lead['case_number']
    name     = lead['deceased_name']

    try:
        if case_exists(case):
            print(f'SKIP (exists): {case} — {name}')
            skipped += 1
            continue

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

        result  = close_req('POST', 'lead', json={
            'name':        f'Estate of {name}',
            'description': f'Case #{case} | Property: ${lead["property_val"]:,.0f} | Equity: ${equity:,.0f} | {rec.get("action","")}',
            'contacts':    contacts,
        })
        lead_id = result['id']
        close_req('POST', 'activity/note', json={'lead_id': lead_id, 'note': note})
        print(f'CREATED: {name} → {lead_id} | {rec.get("action","")}')
        created += 1
        time.sleep(0.5)

    except Exception as e:
        print(f'ERROR on {case}: {e}')
        errors += 1

print(f'\nDone — {created} created, {skipped} skipped, {errors} errors')
