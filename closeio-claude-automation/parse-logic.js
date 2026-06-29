/**
 * Parsing logic for Make.com — use in a "Tools > Set Variable" module
 * or as reference for what Make's JSON module does automatically.
 *
 * In Make: module 3 (json:ParseJSON) handles this automatically.
 * This file exists so you can test / debug the parsing outside Make.
 */

// ─── Raw Claude API response ────────────────────────────────────────────────
const claudeResponse = {
  "id": "msg_01XTbxyz",
  "type": "message",
  "role": "assistant",
  "content": [
    {
      "type": "text",
      "text": "{\"score\":8,\"tier\":\"hot\",\"motivation\":\"Inherited property, wants a fast hassle-free exit without repairs.\",\"timeline\":\"30-60 days\",\"strategy\":\"subject-to\",\"next_action\":\"Call within 24h, offer cash + speed, ask about remaining mortgage balance.\",\"note\":\"Maria inherited a single-family rental in El Cajon from her mother. Property has a paying tenant at $1,400/mo but needs roof and HVAC. She is motivated by speed and simplicity, not price. Strong candidate for subject-to or seller finance with minimal friction.\"}"
    }
  ],
  "model": "claude-sonnet-4-6",
  "usage": { "input_tokens": 312, "output_tokens": 128 }
};

// ─── Step 1: Extract text from content array ─────────────────────────────────
// In Make this is: {{2.body.content[].text}}
const rawText = claudeResponse.content[0].text;

// ─── Step 2: Strip accidental markdown fences (defensive) ────────────────────
// Claude should return raw JSON per the system prompt, but just in case:
const cleaned = rawText
  .replace(/^```json\s*/i, "")
  .replace(/^```\s*/i, "")
  .replace(/\s*```$/i, "")
  .trim();

// ─── Step 3: Parse ───────────────────────────────────────────────────────────
let qualification;
try {
  qualification = JSON.parse(cleaned);
} catch (e) {
  // Fallback: flag for manual review
  qualification = {
    score: 0,
    tier: "unknown",
    motivation: "Parse error — review raw Claude output",
    timeline: "unknown",
    strategy: "skip",
    next_action: "Manual review required: Claude returned non-JSON response",
    note: `Claude parse error. Raw output: ${rawText.slice(0, 300)}`
  };
}

// ─── Step 4: Map tier → Close.io status label ────────────────────────────────
// Adjust these to match your actual Close.io status labels
const STATUS_MAP = {
  hot: "Qualified - Hot",
  warm: "Qualified - Warm",
  cold: "Nurture",
  unknown: "Needs Review"
};

const closeStatus = STATUS_MAP[qualification.tier] ?? "Needs Review";

// ─── Step 5: Build CRM note string ───────────────────────────────────────────
const crmNote = [
  `🤖 AI Qualification`,
  ``,
  `Score: ${qualification.score}/10 | Tier: ${qualification.tier.toUpperCase()}`,
  `Strategy: ${qualification.strategy}`,
  `Timeline: ${qualification.timeline}`,
  `Motivation: ${qualification.motivation}`,
  ``,
  qualification.note,
  ``,
  `Next Action: ${qualification.next_action}`
].join("\n");

// ─── Output (what Make's downstream modules receive) ─────────────────────────
const output = {
  score: qualification.score,
  tier: qualification.tier,
  motivation: qualification.motivation,
  timeline: qualification.timeline,
  strategy: qualification.strategy,
  next_action: qualification.next_action,
  note: qualification.note,
  crm_note: crmNote,
  close_status: closeStatus
};

console.log(JSON.stringify(output, null, 2));

module.exports = { parseClaudeResponse: () => output };
