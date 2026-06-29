#!/bin/bash
# Close.io API helpers for the SMS classification automation
# Replace CLOSEIO_API_KEY with your actual key

API_KEY="REPLACE_CLOSEIO_API_KEY"
BASE_URL="https://api.close.com/api/v1"
AUTH="Authorization: Basic $(echo -n "${API_KEY}:" | base64)"

# ─────────────────────────────────────────────
# 1. LIST CUSTOM FIELDS — find your lcf_ IDs
# ─────────────────────────────────────────────
list_custom_fields() {
  curl -s -X GET \
    "${BASE_URL}/custom_field/lead/" \
    -H "${AUTH}" \
    -H "Content-Type: application/json" \
    | jq '.data[] | {id, name, type, choices}'
}

# ─────────────────────────────────────────────
# 2. LIST STATUSES — confirm status_label values exist
# ─────────────────────────────────────────────
list_statuses() {
  curl -s -X GET \
    "${BASE_URL}/status/lead/" \
    -H "${AUTH}" \
    -H "Content-Type: application/json" \
    | jq '.data[] | {id, label}'
}

# ─────────────────────────────────────────────
# 3. INSPECT A SINGLE ACTIVITY — confirm webhook payload shape
# ─────────────────────────────────────────────
get_activity() {
  ACTIVITY_ID="${1:-acti_REPLACE_WITH_ID}"
  curl -s -X GET \
    "${BASE_URL}/activity/sms/${ACTIVITY_ID}/" \
    -H "${AUTH}" \
    -H "Content-Type: application/json" \
    | jq '{id, _type, direction, body, lead_id, contact_id, date_created}'
}

# ─────────────────────────────────────────────
# 4. UPDATE LEAD — manual test of the write-back
# ─────────────────────────────────────────────
update_lead_classification() {
  LEAD_ID="${1}"
  CLASSIFICATION="${2}"   # e.g. interested
  CONFIDENCE="${3}"       # e.g. 82
  ACTIVITY_ID="${4}"
  # Replace lcf_ values with your actual field IDs from list_custom_fields
  STATUS=$(case "$CLASSIFICATION" in
    not_interested) echo "Not Interested" ;;
    dnc)            echo "DNC" ;;
    maybe_later)    echo "Nurture — Maybe Later" ;;
    *)              echo "AI Qualifying" ;;
  esac)
  curl -s -X PUT \
    "${BASE_URL}/lead/${LEAD_ID}/" \
    -H "${AUTH}" \
    -H "Content-Type: application/json" \
    -d "{
      \"custom.lcf_REPLACE_REPLY_CLASSIFICATION\": \"${CLASSIFICATION}\",
      \"custom.lcf_REPLACE_CLASSIFICATION_CONFIDENCE\": ${CONFIDENCE},
      \"custom.lcf_REPLACE_LAST_PROCESSED_ACTIVITY\": \"${ACTIVITY_ID}\",
      \"status_label\": \"${STATUS}\"
    }" \
    | jq '{id, status_label, custom}'
}

# ─────────────────────────────────────────────
# 5. FETCH RECENT INBOUND SMS — sanity check before going live
# ─────────────────────────────────────────────
recent_inbound_sms() {
  curl -s -X GET \
    "${BASE_URL}/activity/sms/?direction=inbound&_limit=5" \
    -H "${AUTH}" \
    -H "Content-Type: application/json" \
    | jq '.data[] | {id, lead_id, direction, body: .body[:80], date_created}'
}

case "$1" in
  list_custom_fields)        list_custom_fields ;;
  list_statuses)             list_statuses ;;
  get_activity)              get_activity "$2" ;;
  update_lead_classification) update_lead_classification "$2" "$3" "$4" "$5" ;;
  recent_inbound_sms)        recent_inbound_sms ;;
  *)
    echo "Commands:"
    echo "  list_custom_fields"
    echo "  list_statuses"
    echo "  get_activity <activity_id>"
    echo "  update_lead_classification <lead_id> <classification> <confidence> <activity_id>"
    echo "  recent_inbound_sms"
    ;;
esac
