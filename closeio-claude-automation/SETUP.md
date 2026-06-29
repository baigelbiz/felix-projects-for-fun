# Setup Checklist — SMS Classification

Estimated time: 20 min

---

## Step 1 — Create Close.io custom fields (~5 min)

Settings → Custom Fields → Leads → Add Field:

| Field name                   | Type             | Notes                                                   |
|------------------------------|------------------|---------------------------------------------------------|
| `reply_classification`       | Text (choices)   | Choices: not_interested, dnc, maybe_later, price_fishing, interested, motivated, urgent |
| `classification_confidence`  | Number           | Stores 1–100 integer                                    |
| `last_processed_activity`    | Text             | Stores the activity ID (e.g. `acti_XXXXX`)              |

After creating, run to get field IDs:
```bash
bash closeio-api-calls.sh list_custom_fields
```
You need the `lcf_XXXXXXXX` ID for each field.

---

## Step 2 — Confirm lead statuses exist (~2 min)

```bash
bash closeio-api-calls.sh list_statuses
```

Required labels (add in Settings → Lead Statuses if missing):
- `Not Interested`
- `DNC`
- `Nurture — Maybe Later`
- `AI Qualifying`

The em dash in `Nurture — Maybe Later` must match exactly what's in your org.

---

## Step 3 — Import Make.com blueprint (~3 min)

1. Make.com → Create new scenario → `⋯` menu → Import Blueprint
2. Upload `make-blueprint.json`
3. Make will flag all `REPLACE_` tokens — fill them in:

| Token | Value |
|---|---|
| `REPLACE_CLAUDE_API_KEY` | console.anthropic.com → API Keys |
| `REPLACE_CLOSEIO_API_KEY` | Close.io → Settings → API Keys (base64 encode `key:` with empty password) |
| `lcf_REPLACE_REPLY_CLASSIFICATION` | from Step 1 |
| `lcf_REPLACE_CLASSIFICATION_CONFIDENCE` | from Step 1 |
| `lcf_REPLACE_LAST_PROCESSED_ACTIVITY` | from Step 1 |

---

## Step 4 — Wire the webhook in Close.io (~3 min)

1. Click module 1 in Make → Copy webhook URL
2. Close.io → Settings → Integrations → Webhooks → Add Webhook
   - URL: paste Make webhook URL
   - Events: check **`activity.created`** only
   - Leave all other events unchecked

The Make filter (module 2) handles the `_type = SMS` + `direction = inbound` check — Close.io will send all activity events, Make silently drops non-matching ones.

---

## Step 5 — Test end-to-end (~5 min)

1. Send a test inbound SMS to one of your Close.io numbers (or use a real lead)
2. Watch Make → Scenario History for an execution
3. Click the run to confirm:
   - Module 1 received the webhook with `_type: SMS, direction: inbound`
   - Module 2 (Claude) returned a valid JSON classification
   - Module 3 parsed `classification` and `confidence`
   - Module 4 wrote fields and updated `status_label`
4. Open the lead in Close.io and verify all three custom fields populated

To test manually without a live SMS:
```bash
bash closeio-api-calls.sh update_lead_classification lead_XXXXX interested 82 acti_TEST
```

---

## Cost

- Claude claude-sonnet-4-6: ~30 input tokens + ~20 output tokens per SMS ≈ **$0.00015/SMS**
- Make.com: 4 operations per execution
