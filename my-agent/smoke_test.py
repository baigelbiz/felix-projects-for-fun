"""Quick sanity check that the agent can talk to the model without erroring.

Run this after changing the model, API params, or dependencies — it would
have caught the o4-mini/max_tokens 400 error before it reached WhatsApp.

Usage: .venv/bin/python smoke_test.py
"""

import os
import sys

from agent_reply import run

CANNED_PROMPT = "Reply with exactly one word: pong"
# A prompt that forces at least one tool-call round trip — a plain text prompt
# alone never exercises the function-response code path, which is how a bad
# Content.role there (see agent_reply.py) shipped without failing this test.
TOOL_PROMPT = "Save this to memory: the sky is blue. Then tell me you saved it."


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

    try:
        tool_reply, _ = run(TOOL_PROMPT, [])
    except Exception as e:
        print(f"FAIL: agent raised an exception on a tool-calling prompt: {e}")
        return 1

    if tool_reply.startswith(("Error:", "⚠️", "Sorry, I got stuck")):
        print(f"FAIL: agent returned an error reply on a tool-calling prompt: {tool_reply}")
        return 1

    print(f"OK (tool call): {tool_reply}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
