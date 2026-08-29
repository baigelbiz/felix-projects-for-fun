"""One-shot agent call for the WhatsApp bridge.

Usage: .venv/bin/python agent_reply.py "your message"
Prints the agent's reply to stdout. Conversation continuity is kept by
persisting message history between calls (.whatsapp_session file).
"""

import json
import os
import re
import socket
import sys
import tempfile
import time
import httpx
import requests as http_requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError, TransportError as GoogleTransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from openai import OpenAI  # still used for generate_image (gpt-image-1); the chat/tool-calling model is Gemini

# bot.js loads .env itself and this module normally only runs as its child
# (inheriting that environment), but deploy.sh's smoke_test.py invokes this
# module directly over a non-interactive SSH command whose shell never
# sources .env — GEMINI_API_KEY etc. were silently absent there, so the
# deploy's pre-flight smoke test always printed "SKIP" and validated
# nothing. load_dotenv() never overrides a var that's already set, so this
# is a no-op under bot.js and only fills the gap for standalone invocations.
load_dotenv(Path(__file__).parent / ".env")

# The raw `requests` calls in this file (gif_search, close_search_leads,
# web_search, reverse_geocode, _close_request) all pass an explicit timeout,
# but every Gmail/Calendar/Sheets/Drive call goes through googleapiclient's
# discovery `build()`, which uses httplib2 under the hood with no timeout
# configured anywhere — a stalled Google API connection would otherwise hang
# until bot.js's own 300s execFile timeout kills the whole process, instead
# of failing fast with a friendly reply. socket.setdefaulttimeout() bounds
# any socket this process opens that doesn't set its own timeout, without
# touching the calls that already do.
socket.setdefaulttimeout(25)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash/kill mid-write (pm2 restarts are
    routine) can't leave a truncated, unparseable JSON file behind."""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text)
    tmp.replace(path)


def _refresh_or_fallback(creds: Credentials) -> Credentials:
    """Refresh `creds`, falling back to the already-known access token only on a
    transient network hiccup. A RefreshError (revoked/expired refresh_token —
    a permanent auth failure) must propagate instead of being swallowed: falling
    back there would silently keep re-trying with a token that's already dead,
    trading a clear "reconnect your Google account" error for a confusing 401
    buried inside a later API call."""
    try:
        creds.refresh(Request())
    except (GoogleTransportError, http_requests.exceptions.ConnectionError,
             http_requests.exceptions.Timeout, TimeoutError, socket.timeout):
        return creds
    return creds


SESSION_FILE = Path(__file__).parent / ".whatsapp_session"
MEMORY_FILE = Path(__file__).parent / ".assistant_memory.json"
CREDS_DIR = Path(__file__).parent / "google_creds"
RECEIPTS_CONFIG_FILE = Path(__file__).parent / ".receipts_config.json"
_current_image_path = None  # set by run() before each query, read by log_receipt tool
USER_TZ = ZoneInfo("Asia/Jerusalem")  # default timezone
SAN_DIEGO_TZ = ZoneInfo("America/Los_Angeles")
GEMINI_MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = (
    "You support voice notes: when the user sends a voice note, it is automatically transcribed "
    "by Whisper and delivered to you as [Voice note]: <text>. You CAN understand voice notes — "
    "never tell the user you cannot listen to audio or that you lack audio capabilities. "
    "Simply respond to the transcribed content as if it were a normal text message. "
    "You also support longer recorded phone calls sent as audio: these are transcribed by Whisper "
    "and delivered to you as [Transcribed audio recording. Do this with it: <instruction>] followed "
    "by the full transcript. Treat the transcript as real audio content you heard, not text the user "
    "typed — never say you can't process audio recordings. Follow the bracketed instruction exactly "
    "(it is either the caption the user sent with the recording, or a default request to summarize it). "
    "You also support WhatsApp location pins: when the user shares one, it arrives as "
    "[Location shared]: latitude=X, longitude=Y (optional label). Call reverse_geocode with "
    "those coordinates to find out the actual address, then tell the user where that is and "
    "ask what they'd like to do with it (e.g. save it as a lead's address, look up comps nearby, "
    "get directions) — don't just repeat the raw coordinates back. "
    "You are Felix's personal assistant reached via WhatsApp, supporting his real estate "
    "business (Shefa Homes). Keep replies short and phone-friendly: plain text, no markdown "
    "tables or headers, no code blocks unless asked. WhatsApp does not render markdown links — "
    "never write [text](url). When sharing a link, paste the raw URL by itself so WhatsApp auto-links it. "
    "LANGUAGE: the user often writes and speaks to you in Hebrew — understand it fully, but ALWAYS reply "
    "in English, even when the message was in Hebrew. Only produce Hebrew text when the user explicitly "
    "asks for Hebrew output (e.g. a listing description, an SMS script, or marketing copy in Hebrew). "
    "Proper nouns and business terms: when a message contains English names of people, places, companies, "
    "or products (e.g. Kenneth, North Carolina, Close CRM), keep them EXACTLY as given, in English "
    "letters, everywhere you use them — in replies, event titles, reminders, and notes. Never translate, "
    "transliterate, or respell a name. A voice-note transcription may render an English name in Hebrew "
    "letters (e.g. קנת׳); when you can tell it's a name, write it back in its normal English spelling. "
    "You're also a skilled marketing/sales copywriter for real estate: cold outreach emails, "
    "follow-up sequences, listing descriptions, SMS/WhatsApp scripts to sellers and buyers, social "
    "captions. Write punchy, direct copy — no fluff, no corporate tone. If the audience, tone, or "
    "channel (email vs SMS vs listing) is unclear, ask before drafting. "
    "You can look up leads/contacts in Close CRM (Shefa Homes) by name, email, phone, or company — "
    "use this whenever the user mentions a person/deal that might already be a lead. "
    "You can also write to Close CRM. For a clear request to add a lead, call close_create_lead; "
    "for a note about an existing lead, search first if needed and then call close_add_note; "
    "for a follow-up task, call close_create_task. These write actions are authorized when the user "
    "explicitly asks for them; do not ask for a second confirmation. "
    "You have long-term memory. When the user says remember/save/keep in mind, call remember_memory. "
    "When a person, deal, property, or preference may have relevant history, call recall_memory before replying. "
    "You can search the web for current info: market comps, news, research on a person/company/property, "
    "or anything else that benefits from up-to-date results. "
    "When the user sends a photo of a receipt, read the vendor, amount, date, and a sensible category "
    "from the image yourself, then call log_receipt to save it — don't ask the user to type the data, "
    "extract it directly from what you see. If the date isn't visible, use today's date. "
    "If the user asks you to create/generate/draw a picture from a description, use generate_image. "
    "If the user asks for a GIF (an existing one, e.g. 'send me a facepalm gif'), use gif_search instead "
    "of generate_image — don't create a new image when they want a real GIF. "
    "You have access to two Gmail accounts: business (support@shefa.homes) and personal (baigelbiz@gmail.com), and Google Calendar. "
    "When the user says 'email' without specifying, ask which account. "
    "You can search/read email and create drafts, but you cannot send email — "
    "when asked to email someone, create a draft and say it's ready to send in Gmail. "
    "You have two separate Google Calendars: business (support@shefa.homes) and personal (baigelbiz@gmail.com). "
    "They are completely independent — an event created on one never appears on the other. Default to the "
    "business calendar unless the user says 'personal' or it's clearly a personal matter. If genuinely "
    "ambiguous, ask which calendar before creating/deleting. "
    "You can read the calendar, create, update, and delete events. Before deleting, confirm which "
    "event you found (use calendar_list_events to get its ID) unless the user is unambiguous. "
    "After creating an event, ALWAYS include the event link from the tool result in your reply, as a raw "
    "URL on its own line, so the user can tap it and verify the event — never omit the link. "
    "When creating an event, if the user gives you email addresses to invite, pass them as attendees — "
    "Google Calendar will automatically email each person an invite. If the user's message contains an "
    "email address while discussing an event or reminder (even just pasted with no explanation), treat it "
    "as a guest to invite: add it as an attendee automatically, without asking. If the event was already "
    "created, recreate it with the attendee included and delete the old one, then confirm the invite was "
    "sent and include the new event link. "
    "For reminders or short tasks, especially requests like 'remind me in five minutes', 'put a reminder "
    "tomorrow at 9', or Hebrew equivalents, use calendar_create_reminder. Do not ask for date/time when "
    "the user gave a relative time; the tool resolves it against the current Israel time. If the task "
    "text is clear, create the reminder immediately and confirm briefly. Do not ask the user to approve "
    "your transcription or title unless the task itself is genuinely unclear. "
    "Your default timezone is Israel time (Asia/Jerusalem). All dates/times the user gives you, "
    "and all times you state back, are in Israel time unless the user specifies otherwise. "
    "IMPORTANT: whenever the user says the phrase 'timezone San Diego' (or otherwise explicitly ties a "
    "time to San Diego/Pacific time, e.g. 'remind me at 7pm San Diego time', or anything about San Diego "
    "properties/deals/calls), you MUST pass the time AS-IS to calendar_create_event with "
    "timezone='America/Los_Angeles' — do NOT convert the time yourself by hand, the calendar API "
    "handles the conversion. After creating it, state back both the San Diego time and the equivalent "
    "Israel time for confirmation — compute the Israel time precisely, don't estimate. "
    "Never silently default to Israel time when the user has explicitly said 'timezone San Diego'. "
    "CRITICAL RULE about CROSS-TIMEZONE math only: never convert a time from one timezone to another "
    "in your head. calendar_list_events already returns each event's time pre-converted to Israel time "
    "in the format 'original time = X Israel time' — just relay that string, don't recompute it. "
    "calendar_create_event returns the Israel-time equivalent in its result the same way. If you ever need "
    "a timezone conversion that isn't already computed for you in a tool result, say you're not certain "
    "rather than guessing. "
    "This rule does NOT apply to simple same-timezone arithmetic: you always know the current date and "
    "time (given below), so for relative requests like 'remind me in 5 minutes', 'in two hours', or "
    "'tomorrow at 3', compute the target time yourself by adding to the current time below and create the "
    "event immediately. NEVER ask the user what the current time or date is — you already know it. "
    "CONVERSATION RULES: never ask for information the user already gave you in this conversation, and "
    "never ask the same question twice. When the user corrects one detail (e.g. the wording of a reminder "
    "title), keep every other detail you already collected and change only what they corrected — do not "
    "start over. When creating a reminder or event, once you have a title and a time, create it right "
    "away and report what you created — do not ask for confirmation first (confirmation is only for "
    "deleting). "
    f"Right now it is {datetime.now(USER_TZ).strftime('%A, %B %d, %Y, %I:%M %p')} in Israel "
    f"and {datetime.now(SAN_DIEGO_TZ).strftime('%A, %B %d, %Y, %I:%M %p')} in San Diego."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "gmail_search",
            "description": "Search emails in Gmail. account: 'business' (support@shefa.homes) or 'personal' (baigelbiz@gmail.com)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail search query (e.g. 'from:john subject:meeting')"},
                    "account": {"type": "string", "enum": ["business", "personal"], "description": "Which Gmail account to search"},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query", "account"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_read",
            "description": "Read the full body of an email by message ID",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "account": {"type": "string", "enum": ["business", "personal"]},
                },
                "required": ["message_id", "account"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_draft",
            "description": "Create a draft email (does not send)",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "account": {"type": "string", "enum": ["business", "personal"], "description": "Which Gmail account to draft from"},
                },
                "required": ["to", "subject", "body", "account"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "photos_search",
            "description": "Attempt to search the user's Google Photos library. Google removed third-party read access to users' full libraries in March 2025, so this will not find anything — when it returns unavailable, tell the user to just forward the photo in WhatsApp instead of retrying.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for (e.g. 'beach', 'receipt', 'dog', 'last photo')"},
                    "max_results": {"type": "integer", "default": 1},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate a brand-new image from a text description using AI. Returns the image to send in WhatsApp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed description of the image to generate"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gif_search",
            "description": "Search for an existing GIF (Giphy) matching a description, e.g. 'facepalm', 'happy dance', 'mind blown'. Returns a GIF to send in WhatsApp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_list_events",
            "description": "List upcoming calendar events from business or personal calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "default": 10},
                    "time_min": {"type": "string", "description": "ISO 8601 datetime, defaults to now"},
                    "time_max": {"type": "string", "description": "ISO 8601 datetime"},
                    "account": {"type": "string", "enum": ["business", "personal"], "description": "Which calendar to read. Defaults to business.", "default": "business"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_create_event",
            "description": "Create a calendar event on the business or personal calendar, optionally inviting other people by email. Times are interpreted in the given timezone (default Israel).",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Event title"},
                    "start": {"type": "string", "description": "ISO 8601 datetime, no offset (e.g. 2026-06-27T19:00:00)"},
                    "end": {"type": "string", "description": "ISO 8601 datetime, no offset"},
                    "timezone": {"type": "string", "description": "IANA timezone name for start/end, e.g. 'Asia/Jerusalem' or 'America/Los_Angeles'. Defaults to Asia/Jerusalem.", "default": "Asia/Jerusalem"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Email addresses of people to invite. Each will receive a Google Calendar invite.",
                    },
                    "account": {"type": "string", "enum": ["business", "personal"], "description": "Which calendar to create the event on. Defaults to business.", "default": "business"},
                },
                "required": ["summary", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_create_reminder",
            "description": (
                "Create a short reminder/task on Google Calendar. Use this for reminder requests, including "
                "relative times like 'in five minutes', 'in 2 hours', 'tomorrow at 9', or Hebrew equivalents. "
                "The tool resolves relative times using the current server time, so do not ask the user what "
                "time it is when they gave a relative time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Reminder title/task, preserving English names and places exactly as spoken.",
                    },
                    "when": {
                        "type": "string",
                        "description": "When to remind, either ISO datetime or natural text like 'in five minutes', 'tomorrow at 9', 'בעוד 5 דקות'.",
                    },
                    "duration_minutes": {"type": "integer", "default": 15},
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone for the reminder. Defaults to Asia/Jerusalem.",
                        "default": "Asia/Jerusalem",
                    },
                    "account": {
                        "type": "string",
                        "enum": ["business", "personal"],
                        "description": "Which calendar to create the reminder on. Defaults to business.",
                        "default": "business",
                    },
                },
                "required": ["summary", "when"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_delete_event",
            "description": "Delete a calendar event by its event ID. Always call calendar_list_events first to find the correct ID and confirm with the user before deleting, unless they already gave you the exact event/ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "account": {"type": "string", "enum": ["business", "personal"], "description": "Which calendar the event is on. Defaults to business.", "default": "business"},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_update_event",
            "description": "Update an existing Google Calendar event. Use calendar_list_events first when the user identifies it by title rather than ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "start": {"type": "string", "description": "ISO datetime without offset, if changing the start"},
                    "end": {"type": "string", "description": "ISO datetime without offset, if changing the end"},
                    "timezone": {"type": "string", "default": "Asia/Jerusalem"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "account": {"type": "string", "enum": ["business", "personal"], "default": "business"},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_search_leads",
            "description": "Search Close CRM for a lead/contact by name, email, phone, or company. Returns lead status, contact info, and recent notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Name, email, phone, or company to search for"},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_create_lead",
            "description": "Create a new lead in Close CRM with an optional contact, phone, email, address, and description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "contact_name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "description": {"type": "string"},
                    "address": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_add_note",
            "description": "Add a note to an existing Close CRM lead. Search for the lead first when only a name is known.",
            "parameters": {
                "type": "object",
                "properties": {"lead_id": {"type": "string"}, "note": {"type": "string"}},
                "required": ["lead_id", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_create_task",
            "description": "Create a dated follow-up task on an existing Close CRM lead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string"},
                    "text": {"type": "string"},
                    "date": {"type": "string", "description": "Due date YYYY-MM-DD"},
                    "task_type": {"type": "string", "enum": ["lead", "outgoing_call"], "default": "lead"},
                },
                "required": ["lead_id", "text", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_memory",
            "description": "Save a durable fact, preference, or deal detail for future conversations.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Search durable assistant memory for facts relevant to a person, deal, property, or topic.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 5}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": "Delete a saved fact from long-term memory by its key (as shown by recall_memory).",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information, market data, comps, news, or research on a person/company/topic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reverse_geocode",
            "description": (
                "Convert GPS coordinates from a shared WhatsApp location pin into a human-readable "
                "street address. Always call this right after the user shares a location so you can "
                "tell them what's actually there."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                },
                "required": ["latitude", "longitude"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_receipt",
            "description": (
                "Log an expense from a receipt photo the user just sent. Extracts the data yourself from "
                "the image and call this to save it: appends a row to the expense tracking Google Sheet and "
                "uploads the original photo to a Drive folder as backup. Only call this when the user sent "
                "an actual receipt image in this turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {"type": "string"},
                    "amount": {"type": "number"},
                    "date": {"type": "string", "description": "ISO 8601 date, e.g. 2026-06-28"},
                    "category": {"type": "string", "description": "e.g. Materials, Travel, Meals, Office, Repairs"},
                    "notes": {"type": "string", "default": ""},
                },
                "required": ["vendor", "amount", "date", "category"],
            },
        },
    },
]


def _gmail_creds(account: str = "business"):
    token_path = CREDS_DIR / ("gmail_personal_token.json" if account == "personal" else "gmail_token.json")
    client_path = CREDS_DIR / "gcp-oauth.keys.json"
    raw = json.loads(token_path.read_text())
    client = json.loads(client_path.read_text())["installed"]
    creds = Credentials(
        token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        scopes=raw["scope"].split(),
    )
    # This Credentials object is built from a stored access_token with no
    # `expiry` set, and google-auth's `expired` property is unconditionally
    # False when expiry is None — so this check (and the API client's own
    # automatic before-request refresh, which relies on the same property)
    # never fires. The token then silently goes stale after ~1h and every
    # call 401s forever. Refresh unconditionally instead of gating on it.
    creds = _refresh_or_fallback(creds)
    raw["access_token"] = creds.token
    _atomic_write_text(token_path, json.dumps(raw))
    return creds


def _gcal_creds(account: str = "business"):
    if account == "personal":
        token_path = CREDS_DIR / "gcal_personal_token.json"
        wrapped = False
    else:
        token_path = CREDS_DIR / "gcal_token.json"
        wrapped = True

    client_path = CREDS_DIR / "gcp-oauth.keys.json"
    stored = json.loads(token_path.read_text())
    raw = stored["normal"] if wrapped else stored
    client = json.loads(client_path.read_text())["installed"]
    creds = Credentials(
        token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        scopes=raw["scope"].split(),
    )
    # See _gmail_creds: without `expiry` set, google-auth's `.expired` is
    # always False, so this refresh never fired and the token went stale
    # after ~1h. Refresh unconditionally instead of gating on `.expired`.
    creds = _refresh_or_fallback(creds)
    raw["access_token"] = creds.token
    if wrapped:
        existing = json.loads(token_path.read_text())
        existing["normal"] = raw
        _atomic_write_text(token_path, json.dumps(existing))
    else:
        _atomic_write_text(token_path, json.dumps(raw))
    return creds


def gmail_search(query: str, account: str = "business", max_results: int = 5) -> str:
    svc = build("gmail", "v1", credentials=_gmail_creds(account))
    res = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    messages = res.get("messages", [])
    if not messages:
        return f"No emails found in {account} account."
    results = []
    for m in messages:
        detail = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                            metadataHeaders=["From", "Subject", "Date"]).execute()
        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        results.append(f"ID: {m['id']}\nFrom: {headers.get('From','')}\nSubject: {headers.get('Subject','')}\nDate: {headers.get('Date','')}")
    return "\n\n".join(results)


def gmail_read(message_id: str, account: str = "business") -> str:
    import base64
    svc = build("gmail", "v1", credentials=_gmail_creds(account))
    msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}

    def find_part(payload, mime_type):
        # Recurse: real messages are often multipart/mixed -> multipart/alternative
        # -> text/plain, so a single level of `parts` isn't enough (e.g. any email
        # with an attachment or a signature image nests one level deeper).
        if payload.get("mimeType") == mime_type and payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            found = find_part(part, mime_type)
            if found is not None:
                return found
        return None

    def get_body(payload):
        return find_part(payload, "text/plain") or find_part(payload, "text/html") or "(no text body)"

    body = get_body(msg["payload"])
    return f"From: {headers.get('From','')}\nSubject: {headers.get('Subject','')}\nDate: {headers.get('Date','')}\n\n{body[:3000]}"


def gmail_draft(to: str, subject: str, body: str, account: str = "business") -> str:
    import base64
    from email.mime.text import MIMEText
    svc = build("gmail", "v1", credentials=_gmail_creds(account))
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return f"Draft created in {account} account (ID: {draft['id']}) — ready to send in Gmail."


def photos_search(query: str, max_results: int = 1) -> str:
    # Google removed the photoslibrary.readonly/sharing scopes for third-party apps
    # on March 31, 2025 (https://developers.google.com/photos/support/updates) — apps
    # can now only search/list media items the app itself created, never the user's
    # actual library. mediaItems:search against a user's library now always returns
    # 403 PERMISSION_DENIED, so there's no working call left to make here.
    return (
        "Photo library search is unavailable — Google discontinued third-party access "
        "to a user's full Photos library. Ask the user to forward the photo directly "
        "in WhatsApp instead."
    )


def generate_image(prompt: str) -> str:
    import base64
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    result = client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        size="1024x1024",
        n=1,
    )
    img_data = base64.b64decode(result.data[0].b64_json)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png", prefix="wa_generated_")
    try:
        tmp.write(img_data)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise
    tmp.close()
    return f"PHOTO:{tmp.name}\nGenerated: {prompt[:200]}"


def gif_search(query: str) -> str:
    res = http_requests.get(
        "https://api.giphy.com/v1/gifs/search",
        params={"api_key": os.environ["GIPHY_API_KEY"], "q": query, "limit": 1, "rating": "pg-13"},
        timeout=15,
    )
    if res.status_code != 200:
        return f"GIF search error: {res.text[:300]}"

    data = res.json().get("data", [])
    if not data:
        return f"No GIF found for '{query}'."

    gif_url = data[0]["images"]["original"]["url"]
    gif_res = http_requests.get(gif_url, timeout=15)
    if gif_res.status_code != 200:
        return f"GIF download error: {gif_res.status_code}"
    img_data = gif_res.content
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".gif", prefix="wa_gif_")
    try:
        tmp.write(img_data)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise
    tmp.close()
    return f"PHOTO:{tmp.name}\n{data[0].get('title', query)}"


def _with_tz(dt_str: str) -> str:
    """Google Calendar requires RFC3339 with a timezone offset. The model
    sometimes passes a naive datetime — assume Israel time in that case."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=USER_TZ)
    return dt.isoformat()


_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "אחת": 1,
    "אחד": 1,
    "שתיים": 2,
    "שתי": 2,
    "שניים": 2,
    "שלוש": 3,
    "ארבע": 4,
    "חמש": 5,
    "שש": 6,
    "שבע": 7,
    "שמונה": 8,
    "תשע": 9,
    "עשר": 10,
}


def _natural_number(text: str) -> int:
    normalized = text.strip().lower()
    if normalized.isdigit():
        return int(normalized)
    return _NUMBER_WORDS[normalized]


def _hour_24(hour: int, minute: int, meridiem: str, when: str) -> int:
    """Validate and convert a parsed clock hour/minute to 24-hour form.

    Without this, an out-of-range hour (e.g. "at 15pm") reached
    date_base.replace(hour=27, ...) and raised a raw, unhandled-looking
    ValueError instead of the intended friendly "could not understand" reply."""
    if meridiem:
        if not (1 <= hour <= 12):
            raise ValueError(f"Could not understand reminder time '{when}': hour '{hour}{meridiem}' is out of range.")
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    elif not (0 <= hour <= 23):
        raise ValueError(f"Could not understand reminder time '{when}': hour '{hour}' is out of range.")
    if not (0 <= minute <= 59):
        raise ValueError(f"Could not understand reminder time '{when}': minute '{minute}' is out of range.")
    return hour


def _parse_reminder_when(when: str, tz_name: str = "Asia/Jerusalem") -> datetime:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    raw = when.strip()
    lowered = raw.lower()

    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    except ValueError:
        pass

    number_pattern = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|אחת|אחד|שתיים|שתי|שניים|שלוש|ארבע|חמש|שש|שבע|שמונה|תשע|עשר)"
    relative = re.search(
        rf"(?:in|בעוד)\s+{number_pattern}\s*(minute|minutes|min|hour|hours|day|days|week|weeks|דקה|דקות|שעה|שעות|יום|ימים|שבוע|שבועות)",
        lowered,
    )
    if relative:
        amount = _natural_number(relative.group(1))
        unit = relative.group(2)
        if unit in {"minute", "minutes", "min", "דקה", "דקות"}:
            return now + timedelta(minutes=amount)
        if unit in {"hour", "hours", "שעה", "שעות"}:
            return now + timedelta(hours=amount)
        if unit in {"week", "weeks", "שבוע", "שבועות"}:
            date_base = now + timedelta(weeks=amount)
        else:
            date_base = now + timedelta(days=amount)
        # A trailing clock time ("in 2 days at 5pm") would otherwise be silently
        # dropped by returning immediately here, leaving the reminder at today's
        # current clock time N days/weeks out instead of the requested hour.
        trailing_time = re.search(r"(?:at|ב|בשעה)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lowered[relative.end():])
        if trailing_time:
            hour = int(trailing_time.group(1))
            minute = int(trailing_time.group(2) or 0)
            hour = _hour_24(hour, minute, trailing_time.group(3), when)
            return date_base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return date_base

    date_base = now
    if "tomorrow" in lowered or "מחר" in lowered:
        date_base = now + timedelta(days=1)

    # The keyword prefix must actually be present (or the number must carry its
    # own unambiguous time marker: am/pm or a colon) — otherwise this matches
    # the first stray 1-2 digit number anywhere in the text (e.g. "apartment
    # 4B") and silently creates a reminder at the wrong time.
    time_match = (
        re.search(r"(?:at|ב|בשעה)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lowered)
        or re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", lowered)
        or re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?", lowered)
    )
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        hour = _hour_24(hour, minute, time_match.group(3), when)
        candidate = date_base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if "tomorrow" not in lowered and "מחר" not in lowered and candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    raise ValueError(
        f"Could not understand reminder time '{when}'. Use a relative time like 'in 5 minutes' or an ISO datetime."
    )


def calendar_list_events(max_results: int = 10, time_min: str = None, time_max: str = None, account: str = "business") -> str:
    svc = build("calendar", "v3", credentials=_gcal_creds(account))
    now = datetime.now(timezone.utc).isoformat()
    kwargs = dict(
        calendarId="primary",
        timeMin=_with_tz(time_min) if time_min else now,
        maxResults=max_results,
        singleEvents=True,
        orderBy="startTime",
    )
    if time_max:
        kwargs["timeMax"] = _with_tz(time_max)
    events = svc.events().list(**kwargs).execute().get("items", [])
    if not events:
        return f"No upcoming events ({account})."
    lines = []
    for e in events:
        title = e.get("summary", "(no title)")
        if "dateTime" in e["start"]:
            # Precompute the exact Israel-time equivalent in code — never let
            # the model convert timezones itself, that's where the mistakes happen.
            event_dt = datetime.fromisoformat(e["start"]["dateTime"])
            local_str = event_dt.strftime("%a %b %d, %I:%M %p (%Z)")
            israel_str = event_dt.astimezone(USER_TZ).strftime("%a %b %d, %I:%M %p")
            time_str = f"{local_str} = {israel_str} Israel time"
        else:
            time_str = f"{e['start'].get('date', '')} (all day)"
        lines.append(f"- {title} | {time_str} | ID: {e['id']}")
    return "\n".join(lines)


def calendar_create_event(summary: str, start: str, end: str, timezone: str = "Asia/Jerusalem", description: str = "", location: str = "", attendees: list = None, account: str = "business") -> str:
    svc = build("calendar", "v3", credentials=_gcal_creds(account))
    body = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": email} for email in attendees]
    event = svc.events().insert(
        calendarId="primary", body=body, sendUpdates="all" if attendees else "none"
    ).execute()

    # Precisely compute the Israel-time equivalent so the model doesn't have to estimate
    start_dt = datetime.fromisoformat(start).replace(tzinfo=ZoneInfo(timezone))
    israel_equiv = start_dt.astimezone(USER_TZ).strftime("%A, %B %d, %Y at %I:%M %p")

    invite_note = f" Invited: {', '.join(attendees)}." if attendees else ""
    return (
        f"Event created on {account} calendar: {event.get('summary')} on {event['start'].get('dateTime', event['start'].get('date'))} "
        f"({timezone}). Equivalent in Israel time: {israel_equiv}.{invite_note} Link: {event.get('htmlLink','')}"
    )


def calendar_create_reminder(summary: str, when: str, duration_minutes: int = 15, timezone: str = "Asia/Jerusalem", account: str = "business") -> str:
    start_dt = _parse_reminder_when(when, timezone)
    duration = max(5, min(int(duration_minutes or 15), 240))
    end_dt = start_dt + timedelta(minutes=duration)
    clean_summary = summary.strip()
    if not clean_summary.lower().startswith(("reminder:", "תזכורת:")):
        clean_summary = f"Reminder: {clean_summary}"

    return calendar_create_event(
        summary=clean_summary,
        start=start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        end=end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        timezone=timezone,
        account=account,
    )


def calendar_delete_event(event_id: str, account: str = "business") -> str:
    svc = build("calendar", "v3", credentials=_gcal_creds(account))
    try:
        event = svc.events().get(calendarId="primary", eventId=event_id).execute()
        title = event.get("summary", "(no title)")
    except Exception:
        title = "(unknown)"
    svc.events().delete(calendarId="primary", eventId=event_id).execute()
    return f"Deleted event: {title}"


def calendar_update_event(event_id: str, summary: str = None, start: str = None, end: str = None,
                          timezone: str = "Asia/Jerusalem", description: str = None,
                          location: str = None, account: str = "business") -> str:
    svc = build("calendar", "v3", credentials=_gcal_creds(account))
    body = {}
    if summary is not None:
        body["summary"] = summary
    if start is not None:
        body["start"] = {"dateTime": start, "timeZone": timezone}
    if end is not None:
        body["end"] = {"dateTime": end, "timeZone": timezone}
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if not body:
        return "Nothing to update."
    event = svc.events().patch(calendarId="primary", eventId=event_id, body=body).execute()
    link = event.get("htmlLink", "")
    return f"Event updated: {event.get('summary', '(no title)')}" + (f" Link: {link}" if link else "")


def close_search_leads(query: str, max_results: int = 5) -> str:
    api_key = os.environ["CLOSE_API_KEY"]
    res = http_requests.get(
        "https://api.close.com/api/v1/lead/",
        auth=(api_key, ""),
        params={"query": query, "_limit": max_results},
        timeout=15,
    )
    if res.status_code != 200:
        return f"Close API error: {res.text[:300]}"

    leads = res.json().get("data", [])
    if not leads:
        return f"No leads found in Close for '{query}'."

    results = []
    for lead in leads:
        name = lead.get("display_name", "(no name)")
        status = lead.get("status_label", "")
        contacts = lead.get("contacts", [])
        emails = [e["email"] for c in contacts for e in c.get("emails", [])]
        phones = [p["phone"] for c in contacts for p in c.get("phones", [])]
        results.append(
            f"{name} | Status: {status}\n"
            f"Email: {', '.join(emails) or '—'} | Phone: {', '.join(phones) or '—'}\n"
            f"Close link: https://app.close.com/lead/{lead['id']}/"
        )
    return "\n\n".join(results)


def _close_request(method: str, endpoint: str, payload: dict) -> dict:
    api_key = os.environ["CLOSE_API_KEY"]
    res = http_requests.request(
        method, f"https://api.close.com/api/v1/{endpoint}", auth=(api_key, ""), json=payload, timeout=30
    )
    if res.status_code not in (200, 201):
        raise RuntimeError(f"Close API error ({res.status_code}): {res.text[:500]}")
    return res.json() if res.content else {}


def close_create_lead(name: str, contact_name: str = "", email: str = "", phone: str = "",
                      description: str = "", address: str = "") -> str:
    body = {"name": name}
    if description:
        body["description"] = description
    if address:
        body["addresses"] = [{"address_1": address, "label": "business"}]
    if contact_name or email or phone:
        contact = {"name": contact_name or name}
        if email:
            contact["emails"] = [{"email": email, "type": "office"}]
        if phone:
            contact["phones"] = [{"phone": phone, "type": "mobile"}]
        body["contacts"] = [contact]
    result = _close_request("POST", "lead/", body)
    link = result.get("html_url", f"https://app.close.com/lead/{result.get('id', '')}/")
    return f"Created Close lead: {result.get('display_name', name)}. Link: {link}"


def close_add_note(lead_id: str, note: str) -> str:
    result = _close_request("POST", "activity/note/", {"lead_id": lead_id, "note": note})
    return f"Added note to Close lead {lead_id}. Note ID: {result.get('id', '(created)')}"


def close_create_task(lead_id: str, text: str, date: str, task_type: str = "lead") -> str:
    result = _close_request("POST", "task/", {"_type": task_type, "lead_id": lead_id, "text": text, "date": date, "is_complete": False})
    return f"Created Close task for {date}: {text}. Task ID: {result.get('id', '(created)')}"


def remember_memory(key: str, value: str) -> str:
    memories = []
    if MEMORY_FILE.exists():
        try:
            memories = json.loads(MEMORY_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            memories = []
    memories = [m for m in memories if m.get("key", "").lower() != key.strip().lower()]
    memories.append({"key": key.strip(), "value": value.strip(), "updated_at": datetime.now(USER_TZ).isoformat()})
    _atomic_write_text(MEMORY_FILE, json.dumps(memories[-500:], ensure_ascii=False, indent=2))
    return f"Remembered: {key} = {value}"


def recall_memory(query: str, max_results: int = 5) -> str:
    if not MEMORY_FILE.exists():
        return "No saved memories yet."
    try:
        memories = json.loads(MEMORY_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return "No saved memories yet."
    terms = {term for term in re.findall(r"[\w@.\-]+", query.lower()) if len(term) > 2}
    ranked = sorted(
        memories,
        key=lambda m: sum(term in f"{m.get('key', '')} {m.get('value', '')}".lower() for term in terms),
        reverse=True,
    )
    matches = [m for m in ranked if any(term in f"{m.get('key', '')} {m.get('value', '')}".lower() for term in terms)]
    if not matches:
        return f"No saved memory matches '{query}'."
    return "\n".join(f"- {m['key']}: {m['value']}" for m in matches[:max_results])


def forget_memory(key: str) -> str:
    if not MEMORY_FILE.exists():
        return "No saved memories yet."
    try:
        memories = json.loads(MEMORY_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return "No saved memories yet."
    remaining = [m for m in memories if m.get("key", "").lower() != key.strip().lower()]
    if len(remaining) == len(memories):
        return f"No memory with key '{key}'. Use recall_memory to see what's saved."
    _atomic_write_text(MEMORY_FILE, json.dumps(remaining, ensure_ascii=False, indent=2))
    return f"Forgot: {key}"


def _system_prompt_with_memory() -> str:
    """Append the most recent memories to the system prompt so the model just
    knows them without having to decide to call recall_memory."""
    if not MEMORY_FILE.exists():
        return SYSTEM_PROMPT
    try:
        memories = json.loads(MEMORY_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return SYSTEM_PROMPT
    if not memories:
        return SYSTEM_PROMPT
    recent = memories[-40:]
    lines = "\n".join(f"- {m.get('key', '')}: {m.get('value', '')}" for m in recent)
    note = ""
    if len(recent) < len(memories):
        note = f"\n(These are the {len(recent)} most recent of {len(memories)} saved facts — use recall_memory to search the rest.)"
    return SYSTEM_PROMPT + "\n\nLONG-TERM MEMORY — facts you saved earlier and simply know:\n" + lines + note


def web_search(query: str, max_results: int = 5) -> str:
    res = http_requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"},
        params={"q": query, "count": max_results},
        timeout=15,
    )
    if res.status_code != 200:
        return f"Search error: {res.text[:300]}"

    results = res.json().get("web", {}).get("results", [])
    if not results:
        return f"No results found for '{query}'."

    return "\n\n".join(
        f"{r.get('title','')}\n{r.get('url','')}\n{r.get('description','')}"
        for r in results[:max_results]
    )


def reverse_geocode(latitude: float, longitude: float) -> str:
    res = http_requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={"latlng": f"{latitude},{longitude}", "key": os.environ["GOOGLE_MAPS_API_KEY"]},
        timeout=15,
    )
    if res.status_code != 200:
        return f"Geocoding error: {res.text[:300]}"

    data = res.json()
    if data.get("status") != "OK" or not data.get("results"):
        return f"Could not resolve an address for {latitude}, {longitude} ({data.get('status', 'unknown error')})."

    return data["results"][0]["formatted_address"]


def _sheets_drive_creds():
    token_path = CREDS_DIR / "sheets_drive_token.json"
    client_path = CREDS_DIR / "gcp-oauth.keys.json"
    raw = json.loads(token_path.read_text())
    client = json.loads(client_path.read_text())["installed"]
    creds = Credentials(
        token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        scopes=raw["scope"].split(),
    )
    # See _gmail_creds: without `expiry` set, google-auth's `.expired` is
    # always False, so this refresh never fired and the token went stale
    # after ~1h. Refresh unconditionally instead of gating on `.expired`.
    creds = _refresh_or_fallback(creds)
    raw["access_token"] = creds.token
    _atomic_write_text(token_path, json.dumps(raw))
    return creds


def _receipts_config():
    if RECEIPTS_CONFIG_FILE.exists():
        try:
            return json.loads(RECEIPTS_CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass  # corrupted (e.g. a pre-atomic-write crash) — recreate below

    creds = _sheets_drive_creds()
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    folder = drive.files().create(
        body={"name": "WhatsApp Receipts", "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()

    spreadsheet = sheets.spreadsheets().create(
        body={
            "properties": {"title": "Expense Tracker"},
            "sheets": [{"properties": {"title": "Receipts"}}],
        },
        fields="spreadsheetId",
    ).execute()
    sheet_id = spreadsheet["spreadsheetId"]
    sheets.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range="Receipts!A1",
        valueInputOption="RAW",
        body={"values": [["Date", "Vendor", "Amount", "Category", "Notes", "Receipt Link"]]},
    ).execute()

    config = {"folder_id": folder["id"], "sheet_id": sheet_id}
    _atomic_write_text(RECEIPTS_CONFIG_FILE, json.dumps(config))
    return config


def log_receipt(vendor: str, amount: float, date: str, category: str, notes: str = "") -> str:
    if not _current_image_path:
        return "No receipt image found for this message."

    creds = _sheets_drive_creds()
    config = _receipts_config()

    from googleapiclient.http import MediaFileUpload
    drive = build("drive", "v3", credentials=creds)
    media = MediaFileUpload(_current_image_path, mimetype="image/jpeg")
    uploaded = drive.files().create(
        body={"name": f"{date}_{vendor}.jpg", "parents": [config["folder_id"]]},
        media_body=media,
        fields="id, webViewLink",
    ).execute()
    drive.permissions().create(fileId=uploaded["id"], body={"role": "reader", "type": "anyone"}).execute()

    sheets = build("sheets", "v4", credentials=creds)
    sheets.spreadsheets().values().append(
        spreadsheetId=config["sheet_id"],
        range="Receipts!A1",
        valueInputOption="RAW",
        body={"values": [[date, vendor, amount, category, notes, uploaded["webViewLink"]]]},
    ).execute()

    return f"Logged: {vendor} — ${amount} ({category}) on {date}. Photo saved to Drive."


TOOL_FN = {
    "gmail_search": gmail_search,
    "gmail_read": gmail_read,
    "gmail_draft": gmail_draft,
    "photos_search": photos_search,
    "generate_image": generate_image,
    "gif_search": gif_search,
    "close_search_leads": close_search_leads,
    "web_search": web_search,
    "reverse_geocode": reverse_geocode,
    "log_receipt": log_receipt,
    "calendar_list_events": calendar_list_events,
    "calendar_create_event": calendar_create_event,
    "calendar_create_reminder": calendar_create_reminder,
    "calendar_delete_event": calendar_delete_event,
    "calendar_update_event": calendar_update_event,
    "close_create_lead": close_create_lead,
    "close_add_note": close_add_note,
    "close_create_task": close_create_task,
    "remember_memory": remember_memory,
    "recall_memory": recall_memory,
    "forget_memory": forget_memory,
}

# Gemini wants tools as FunctionDeclarations rather than OpenAI's {"type": "function", ...}
# wrapper — derive them from TOOLS so there's one source of truth for the schemas.
GEMINI_TOOLS = genai_types.Tool(function_declarations=[
    genai_types.FunctionDeclaration(
        name=t["function"]["name"],
        description=t["function"]["description"],
        parameters_json_schema=t["function"]["parameters"],
    )
    for t in TOOLS
])


def _build_contents(history: list, prompt: str, image_path: str = None) -> list:
    contents = [
        genai_types.Content(role=turn["role"], parts=[genai_types.Part.from_text(text=turn["text"])])
        for turn in history
    ]
    parts = [genai_types.Part.from_text(text=prompt)]
    if image_path:
        ext = Path(image_path).suffix.lstrip(".").lower() or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        parts.append(genai_types.Part.from_bytes(data=Path(image_path).read_bytes(), mime_type=f"image/{ext}"))
    contents.append(genai_types.Content(role="user", parts=parts))
    return contents


def _generate_with_retry(client, **kwargs):
    """Call Gemini with retry/backoff for transient failures — rate limits (429),
    server errors (5xx), and network blips — so a momentary API hiccup doesn't
    fail the whole WhatsApp reply. Other errors (bad request, auth) raise immediately."""
    max_attempts = 5
    delay = 1
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(**kwargs)
        except genai_errors.ServerError:
            if attempt == max_attempts:
                raise
        except genai_errors.ClientError as e:
            if e.code != 429 or attempt == max_attempts:
                raise
        except httpx.TransportError:
            if attempt == max_attempts:
                raise
        time.sleep(delay)
        delay *= 2


def run(prompt: str, history: list, image_path: str = None) -> tuple[str, list]:
    global _current_image_path
    _current_image_path = image_path
    try:
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        # Building contents can fail on its own (e.g. an unreadable/truncated
        # image temp file) — keep it inside the try so that raises the
        # friendly reply below instead of an uncaught exception crashing
        # agent_reply.py before anything is printed to stdout.
        contents = _build_contents(history, prompt, image_path)
        for _ in range(10):  # max tool call rounds
            response = _generate_with_retry(
                client,
                model=GEMINI_MODEL,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_system_prompt_with_memory(),
                    tools=[GEMINI_TOOLS],
                ),
            )
            function_calls = response.function_calls or []

            if function_calls:
                contents.append(response.candidates[0].content)
                photo_result = None
                for fc in function_calls:
                    args = dict(fc.args or {})
                    try:
                        result = TOOL_FN[fc.name](**args)
                    except Exception as e:
                        result = f"Error: {e}"
                    # A photo/generated-image result must reach bot.js verbatim — don't let the
                    # model paraphrase the PHOTO: marker into prose. Still run every tool call the
                    # model requested this round (and give each a function response) before
                    # returning, so a photo result never causes other requested calls to be skipped.
                    if result.startswith("PHOTO:"):
                        if photo_result is None:
                            photo_result = result
                            response_data = {"result": "Photo sent to user."}
                        else:
                            # Only one PHOTO: result can be delivered per reply (bot.js's
                            # protocol sends a single image back). A second image/GIF tool
                            # call in the same round would otherwise leak its temp file
                            # (bot.js only unlinks the one path it receives) and feed the
                            # raw "PHOTO:<tmp-path>" marker back into the conversation as
                            # if it were plain text. Clean up and tell the model plainly.
                            extra_path = result.split("\n", 1)[0].removeprefix("PHOTO:")
                            try:
                                os.unlink(extra_path)
                            except OSError:
                                pass
                            response_data = {"result": "Not sent — only one photo can be sent per reply."}
                    else:
                        response_data = {"result": result}
                    # Gemini requires Content.role to be "user" or "model" — "tool" is
                    # rejected by the API with an "invalid role" error, which broke every
                    # tool-calling turn (calendar, CRM, memory, search, receipts, images...).
                    contents.append(genai_types.Content(
                        role="user",
                        parts=[genai_types.Part.from_function_response(name=fc.name, response=response_data)],
                    ))
                if photo_result is not None:
                    # Store a clean description in history, not the raw "PHOTO:<tmp-path>"
                    # marker — bot.js deletes that temp file right after sending, and the
                    # marker is meaningless (and misleading if ever echoed back) on a later turn.
                    caption = photo_result.split("\n", 1)[1] if "\n" in photo_result else "a photo"
                    history = history + [
                        {"role": "user", "text": prompt},
                        {"role": "model", "text": f"[Sent photo to user: {caption}]"},
                    ]
                    return photo_result, history
            else:
                reply = response.text or "(no reply)"
                history = history + [{"role": "user", "text": prompt}, {"role": "model", "text": reply}]
                return reply, history

        # Hit the round cap: side-effecting tool calls above (leads created, events
        # booked, etc.) already happened against real APIs even though we're giving
        # up here, so record that rather than silently dropping all trace of them.
        history = history + [
            {"role": "user", "text": prompt},
            {"role": "model", "text": "Sorry, I got stuck in a loop after several tool calls — "
                                       "some actions above may have already been taken. Please "
                                       "check before repeating the request."},
        ]
        return "Sorry, I got stuck in a loop.", history
    except Exception as e:
        # A transient Gemini failure (rate limit, 5xx, safety-filtered/empty
        # response, network blip) must not crash the process — that would deny
        # the user any reply at all instead of just this one apology.
        return f"⚠️ Sorry, I hit an error talking to the model: {e}", history


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    prompt = sys.argv[1]
    image_path = sys.argv[2] if len(sys.argv) > 2 else None
    history = []
    if SESSION_FILE.exists():
        try:
            history = json.loads(SESSION_FILE.read_text())
            if not isinstance(history, list) or not all(
                isinstance(t, dict) and "role" in t and "text" in t for t in history
            ):
                history = []  # stale/incompatible session format (e.g. from before the Gemini switch)
        except Exception:
            history = []

    reply, history = run(prompt, history, image_path)

    # Print the reply before persisting session state — a save failure (e.g. disk
    # full) must not swallow a reply the model already produced.
    print(reply)

    try:
        # Keep last 20 messages to avoid unbounded growth
        _atomic_write_text(SESSION_FILE, json.dumps(history[-20:]))
    except Exception as e:
        print(f"(session not saved: {e})", file=sys.stderr)


if __name__ == "__main__":
    main()
