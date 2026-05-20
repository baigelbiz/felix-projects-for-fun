#!/usr/bin/env python3
"""
LinkedIn Auto-Poster Agent for Felix Baigel
Builds San Diego wholesaling / creative finance brand daily.

Usage:
  python3 linkedin_agent.py --auth        # One-time LinkedIn login (run from Mac)
  python3 linkedin_agent.py --preview     # See today's post without publishing
  python3 linkedin_agent.py --post        # Generate + publish to LinkedIn
  python3 linkedin_agent.py --schedule    # Run daily at 8:00 AM (keeps running)
"""

import argparse, json, os, sys, time, webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
import requests

# ── env ──────────────────────────────────────────────────────────────────────
for line in open(Path(__file__).parent / '.env'):
    if '=' in line and not line.startswith('#'):
        k, v = line.strip().split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

GEMINI_API_KEY     = os.environ.get('GEMINI_API_KEY', '')
LI_CLIENT_ID       = os.environ.get('LINKEDIN_CLIENT_ID', '')
LI_CLIENT_SECRET   = os.environ.get('LINKEDIN_CLIENT_SECRET', '')
LI_TOKEN_FILE      = Path(__file__).parent / os.environ.get('LINKEDIN_TOKEN_FILE', 'linkedin_token.json')
REDIRECT_URI       = 'http://localhost:8091/callback'
LI_AUTH_URL        = 'https://www.linkedin.com/oauth/v2/authorization'
LI_TOKEN_URL       = 'https://www.linkedin.com/oauth/v2/accessToken'
LI_API             = 'https://api.linkedin.com/v2'
LI_REST_API        = 'https://api.linkedin.com/rest'

# ── topic calendar (cycles every 14 days) ────────────────────────────────────
TOPICS = [
    {
        "theme": "origin story",
        "angle": "Felix built a medical spa, used the cash flow to buy his first real estate deal, and now runs both while building a creative finance portfolio in San Diego.",
        "cta": "Follow along — this is the journey.",
    },
    {
        "theme": "Subject-To explained",
        "angle": "What Subject-To financing is, why distressed sellers say yes, and how the buyer takes over payments without a new loan.",
        "cta": "Drop a question in the comments if you want to know more.",
    },
    {
        "theme": "probate leads",
        "angle": "What probate means, why heirs often want fast cash, how to find and approach probate leads respectfully in San Diego.",
        "cta": "Have you ever worked a probate deal? Share your experience.",
    },
    {
        "theme": "Seller Finance breakdown",
        "angle": "How Seller Finance creates monthly cash flow with none of your own money, structured as a real example with made-up numbers.",
        "cta": "Save this post if you're learning creative finance.",
    },
    {
        "theme": "San Diego market insight",
        "angle": "Why high-priced markets like San Diego are actually ideal for creative finance — equity-rich sellers, motivated by life events not just price.",
        "cta": "What's your read on the San Diego market right now?",
    },
    {
        "theme": "lessons from running a med spa",
        "angle": "Three things running a medical spa taught Felix about negotiating with sellers: speed, empathy, and solving the real problem.",
        "cta": "Business owners make great investors. Here's why.",
    },
    {
        "theme": "wholesaling vs wholetailing",
        "angle": "The difference between assigning a contract and light-renovating before selling, when to use each, and how to decide.",
        "cta": "Which strategy fits your market?",
    },
    {
        "theme": "finding deals with SMS outreach",
        "angle": "How consistent SMS follow-up to motivated sellers turns cold contacts into warm conversations and eventually deals.",
        "cta": "What's your outreach method? Let's compare notes.",
    },
    {
        "theme": "creative finance myth-busting",
        "angle": "Three myths about creative finance that keep new investors stuck: it's illegal, sellers won't do it, you need experience.",
        "cta": "Which myth held you back the longest?",
    },
    {
        "theme": "building a cash buyer list",
        "angle": "Why your buyer list is your most valuable asset in wholesaling and how to build one in San Diego from zero.",
        "cta": "Are you a cash buyer in SoCal? Let's connect.",
    },
    {
        "theme": "due diligence on a creative deal",
        "angle": "The five things Felix checks before locking up any creative finance deal: equity, liens, motivation, title, and loan status.",
        "cta": "What's the first thing you check?",
    },
    {
        "theme": "the mindset shift",
        "angle": "Why most people think they need a bank's permission to invest in real estate — and why that belief costs them years.",
        "cta": "Tag someone who needs to hear this.",
    },
    {
        "theme": "a deal story",
        "angle": "A fictionalized but realistic story of a San Diego probate lead: the seller's situation, the deal structure, the outcome. Teach through narrative.",
        "cta": "Real estate is a people business. Never forget that.",
    },
    {
        "theme": "what's next",
        "angle": "Felix's goals for the next 12 months: number of deals, types of creative finance structures, building a San Diego network of investors.",
        "cta": "Who else in San Diego is building through creative finance? Let's connect.",
    },
]

# ── Gemini post generation ────────────────────────────────────────────────────
def generate_post(topic: dict) -> str:
    if not GEMINI_API_KEY:
        sys.exit('ERROR: GEMINI_API_KEY not set in .env')

    import google.genai as genai
    from google.genai import types

    system = (
        "You are the ghostwriter for Felix Baigel — a San Diego real estate investor and medical spa owner "
        "who specializes in creative finance (Subject-To, Seller Finance, probate leads). "
        "Write in first person as Felix. Voice: confident, educational, warm, never braggy. "
        "Felix is building his personal brand on LinkedIn to become well-known in San Diego for creative finance and wholesaling. "
        "Every post teaches something real and ends with a question or CTA to drive engagement."
    )

    prompt = f"""Write a LinkedIn post for Felix on this topic:

Theme: {topic['theme']}
Angle: {topic['angle']}
CTA: {topic['cta']}

Rules:
- 150-250 words max
- No hashtag spam — max 4 relevant hashtags at the end
- Hook on line 1 (bold or punchy statement, no emoji opener)
- Short paragraphs (1-3 lines each), lots of white space
- End with the CTA as a direct question or statement
- Include 2-3 line breaks between sections for LinkedIn readability
- Sound like a real person, not a content robot

Return only the post text. No explanation."""

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.7,
        ),
    )
    return response.text.strip()


# ── LinkedIn OAuth ────────────────────────────────────────────────────────────
_auth_code_holder = {}

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/callback':
            params = parse_qs(parsed.query)
            _auth_code_holder['code'] = params.get('code', [None])[0]
            _auth_code_holder['error'] = params.get('error', [None])[0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h2>LinkedIn connected! You can close this tab.</h2>')
    def log_message(self, *args): pass


def linkedin_auth():
    if not LI_CLIENT_ID or not LI_CLIENT_SECRET:
        print('ERROR: Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in .env')
        print('\nSteps:')
        print('1. Go to https://www.linkedin.com/developers/apps')
        print('2. Create an app (or use existing)')
        print('3. Under Auth tab, add redirect URI: http://localhost:8091/callback')
        print('4. Copy Client ID + Client Secret into .env')
        sys.exit(1)

    params = {
        'response_type': 'code',
        'client_id': LI_CLIENT_ID,
        'redirect_uri': REDIRECT_URI,
        'scope': 'openid profile w_member_social',
    }
    url = f'{LI_AUTH_URL}?{urlencode(params)}'
    print(f'\nOpening LinkedIn login...\n{url}\n')
    webbrowser.open(url)

    server = HTTPServer(('localhost', 8091), OAuthCallbackHandler)
    print('Waiting for LinkedIn authorization...')
    while 'code' not in _auth_code_holder and 'error' not in _auth_code_holder:
        server.handle_request()

    if _auth_code_holder.get('error'):
        sys.exit(f'LinkedIn auth error: {_auth_code_holder["error"]}')

    code = _auth_code_holder['code']
    r = requests.post(LI_TOKEN_URL, data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT_URI,
        'client_id': LI_CLIENT_ID,
        'client_secret': LI_CLIENT_SECRET,
    })
    r.raise_for_status()
    token_data = r.json()

    # get person URN
    me = requests.get(f'{LI_API}/userinfo', headers={'Authorization': f'Bearer {token_data["access_token"]}'})
    me.raise_for_status()
    me_data = me.json()
    token_data['person_urn'] = f'urn:li:person:{me_data["sub"]}'
    token_data['name'] = me_data.get('name', '')

    LI_TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    print(f'\nLinkedIn connected as: {token_data["name"]}')
    print(f'Token saved to {LI_TOKEN_FILE}')


def load_token() -> dict:
    if not LI_TOKEN_FILE.exists():
        sys.exit('ERROR: LinkedIn not connected. Run: python3 linkedin_agent.py --auth')
    return json.loads(LI_TOKEN_FILE.read_text())


def post_to_linkedin(text: str, token: dict) -> str:
    headers = {
        'Authorization': f'Bearer {token["access_token"]}',
        'Content-Type': 'application/json',
        'X-Restli-Protocol-Version': '2.0.0',
        'LinkedIn-Version': '202401',
    }
    payload = {
        'author': token['person_urn'],
        'commentary': text,
        'visibility': 'PUBLIC',
        'distribution': {
            'feedDistribution': 'MAIN_FEED',
            'targetEntities': [],
            'thirdPartyDistributionChannels': [],
        },
        'lifecycleState': 'PUBLISHED',
        'isReshareDisabledByAuthor': False,
    }
    r = requests.post(f'{LI_REST_API}/posts', json=payload, headers=headers)
    r.raise_for_status()
    post_id = r.headers.get('x-restli-id', 'unknown')
    return post_id


# ── topic selection (rotates by day of year) ─────────────────────────────────
def todays_topic() -> dict:
    day_index = date.today().timetuple().tm_yday
    return TOPICS[day_index % len(TOPICS)]


# ── main ─────────────────────────────────────────────────────────────────────
def cmd_preview():
    topic = todays_topic()
    print(f'\nTopic: {topic["theme"]}\n{"─"*50}')
    post = generate_post(topic)
    print(post)
    print(f'\n{"─"*50}')
    print('(Preview only — not posted)')


def cmd_post():
    token = load_token()
    topic = todays_topic()
    print(f'Generating post on: {topic["theme"]}...')
    post = generate_post(topic)
    print(f'\n{"─"*50}\n{post}\n{"─"*50}')
    post_id = post_to_linkedin(post, token)
    print(f'\nPosted to LinkedIn! ID: {post_id}')


def cmd_schedule():
    import schedule as sched
    print('Scheduler running — posts daily at 08:00 AM. Press Ctrl+C to stop.')

    def job():
        try:
            cmd_post()
        except Exception as e:
            print(f'Post failed: {e}')

    sched.every().day.at('08:00').do(job)
    while True:
        sched.run_pending()
        time.sleep(30)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auth',     action='store_true', help='One-time LinkedIn OAuth login')
    parser.add_argument('--preview',  action='store_true', help='Generate post, print it, do not publish')
    parser.add_argument('--post',     action='store_true', help='Generate and publish post now')
    parser.add_argument('--schedule', action='store_true', help='Run daily at 08:00 AM')
    args = parser.parse_args()

    if args.auth:
        linkedin_auth()
    elif args.preview:
        cmd_preview()
    elif args.post:
        cmd_post()
    elif args.schedule:
        cmd_schedule()
    else:
        parser.print_help()
