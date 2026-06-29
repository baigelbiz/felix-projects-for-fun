"""A basic Claude Agent SDK starter.

Demonstrates the common building blocks:
  - a custom tool (defined with @tool, served via an in-process MCP server)
  - built-in tools (Read, Glob) auto-approved through allowed_tools
  - streaming message handling (text, tool calls, final result with cost)
  - error handling for the common SDK failure modes

Run with:  .venv/bin/python main.py
Auth: uses your existing Claude Code login automatically
(or set ANTHROPIC_API_KEY to bill a specific API key).
"""

import asyncio
from typing import Annotated, Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)


# --- Custom tool -------------------------------------------------------------
# @tool(name, description, input_schema) turns an async function into a tool.
# Claude sees it as "mcp__<server>__<tool>" — here: mcp__utils__roll_dice.
@tool(
    "roll_dice",
    "Roll N six-sided dice and return the results",
    {"count": Annotated[int, "Number of six-sided dice to roll"]},
)
async def roll_dice(args: dict[str, Any]) -> dict[str, Any]:
    import random

    rolls = [random.randint(1, 6) for _ in range(args["count"])]
    return {
        "content": [
            {"type": "text", "text": f"Rolled {rolls}, total {sum(rolls)}"}
        ]
    }


# An in-process MCP server hosts your custom tools — no separate process needed.
utils_server = create_sdk_mcp_server(name="utils", version="1.0.0", tools=[roll_dice])


# --- Agent configuration -----------------------------------------------------
options = ClaudeAgentOptions(
    system_prompt="You are a concise assistant. Keep answers short.",
    mcp_servers={"utils": utils_server},
    # Tools listed here run without permission prompts. Anything else
    # (e.g. Write, Bash) would require approval or a permission_mode change.
    allowed_tools=["Read", "Glob", "mcp__utils__roll_dice"],
    max_turns=10,  # safety cap on agentic back-and-forth
)


async def main() -> None:
    prompt = (
        "Roll 3 dice with the roll_dice tool, then list the Python files "
        "in this directory and say what each one is for."
    )

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text)
                    elif isinstance(block, ToolUseBlock):
                        # Progress trace: see which tool the agent is calling
                        print(f"  [tool: {block.name} {block.input}]")
            elif isinstance(message, ResultMessage):
                print(
                    f"\n--- done in {message.num_turns} turns, "
                    f"${message.total_cost_usd:.4f} ---"
                )
    except CLINotFoundError:
        print("Claude Code not found — install it: npm install -g @anthropic-ai/claude-code")
    except ProcessError as e:
        print(f"Agent process failed (exit {e.exit_code}): {e}")


if __name__ == "__main__":
    asyncio.run(main())
