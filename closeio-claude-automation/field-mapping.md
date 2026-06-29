# Field Mapping: Close.io → Make → Claude → Close.io

## Close.io `activity.created` webhook payload → Make module 1

| Webhook field         | Make variable         | Used in                          |
|-----------------------|-----------------------|----------------------------------|
| `data._type`          | `{{1.data._type}}`    | Filter: must equal `SMS`         |
| `data.direction`      | `{{1.data.direction}}`| Filter: must equal `inbound`     |
| `data.body`           | `{{1.data.body}}`     | Claude user message              |
| `data.lead_id`        | `{{1.data.lead_id}}`  | Close.io update URL              |
| `data.id`             | `{{1.data.id}}`       | Stored in `last_processed_activity` |

## Claude response → Make module 3 (json:ParseJSON)

| Claude field       | Make variable        | Type    | Values                                                                  |
|--------------------|----------------------|---------|-------------------------------------------------------------------------|
| `classification`   | `{{3.classification}}`| string  | not_interested / dnc / maybe_later / price_fishing / interested / motivated / urgent |
| `confidence`       | `{{3.confidence}}`   | integer | 1–100                                                                   |

## Make module 4 → Close.io lead fields

Create these three custom fields in Close.io before going live.

| Custom field name            | Type             | Close.io field ID               | Make placeholder                        |
|------------------------------|------------------|---------------------------------|-----------------------------------------|
| `reply_classification`       | Text (choices)   | run `list_custom_fields` to find | `lcf_REPLACE_REPLY_CLASSIFICATION`      |
| `classification_confidence`  | Number           | run `list_custom_fields` to find | `lcf_REPLACE_CLASSIFICATION_CONFIDENCE` |
| `last_processed_activity`    | Text             | run `list_custom_fields` to find | `lcf_REPLACE_LAST_PROCESSED_ACTIVITY`   |

### Choices for `reply_classification` field
Add exactly these values in Close.io (Settings → Custom Fields → edit field → Choices):
`not_interested`, `dnc`, `maybe_later`, `price_fishing`, `interested`, `motivated`, `urgent`

## Classification → Status mapping

| `classification`  | `status_label`            |
|-------------------|---------------------------|
| `not_interested`  | Not Interested            |
| `dnc`             | DNC                       |
| `maybe_later`     | Nurture — Maybe Later     |
| `price_fishing`   | AI Qualifying             |
| `interested`      | AI Qualifying             |
| `motivated`       | AI Qualifying             |
| `urgent`          | AI Qualifying             |

Run `bash closeio-api-calls.sh list_statuses` to confirm these labels exist in your org.
Create any that are missing in Close.io → Settings → Lead Statuses.
