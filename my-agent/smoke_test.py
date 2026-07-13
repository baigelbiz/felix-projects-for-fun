"""Quick sanity check that the agent can talk to the model without erroring.

Run this after changing the model, API params, or dependencies — it would
have caught the o4-mini/max_tokens 400 error before it reached WhatsApp.

Usage: .venv/bin/python smoke_test.py
"""

import os
import sys

from agent_reply import run

CANNED_PROMPT = "Reply with exactly one word: pong"


def main() -> int:
    if "GEMINI_API_KEY" not in os.environ:
        print("SKIP: GEMINI_API_KEY not set in this shell (bot.js normally supplies it via .env)")
        return 0

    try:
        reply, _ = run(CANNED_PROMPT, [])
    except Exception as e:
        print(f"FAIL: agent raised an exception: {e}")
        return 1

    if reply.startswith(("Error:", "⚠️", "Sorry, I got stuck")):
        print(f"FAIL: agent returned an error reply: {reply}")
        return 1

    print(f"OK: {reply}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
