#!/usr/bin/env bash
# MA Hunting Calendar Sync — HostGator setup
# Run this once via SSH to install dependencies and configure the cron job.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# ── 1. Create a Python virtual environment ────────────────────────────────────
echo "==> Creating virtual environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
echo "    Done — $("$VENV_DIR/bin/python" --version)"

# ── 2. Create .env if missing ─────────────────────────────────────────────────
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    sed -i "s|GOOGLE_APPLICATION_CREDENTIALS=.*|GOOGLE_APPLICATION_CREDENTIALS=${SCRIPT_DIR}/service-account-key.json|" "$SCRIPT_DIR/.env"
    echo ""
    echo "ACTION REQUIRED: Edit .env and set your calendar IDs:"
    echo "  $SCRIPT_DIR/.env"
    echo ""
    echo "  HUNTING_CALENDAR_ID           — Google Calendar ID for hunting seasons"
    echo "  FURBEARER_CALENDAR_ID         — Google Calendar ID for furbearer/trapping seasons"
    echo "  MIGRATORY_CALENDAR_ID         — Google Calendar ID for migratory bird seasons"
    echo "  GOOGLE_APPLICATION_CREDENTIALS — already set to ${SCRIPT_DIR}/service-account-key.json"
    echo "                                   (drop the JSON key file there)"
    echo ""
fi

# ── 3. Make run.sh executable ─────────────────────────────────────────────────
chmod +x "$SCRIPT_DIR/run.sh"

# ── 4. Test the script manually before setting up cron ───────────────────────
echo "==> Running a test sync (check output for errors)..."
"$SCRIPT_DIR/run.sh"

# ── 5. Install the cron job ───────────────────────────────────────────────────
# Runs on the 1st of each month at 07:00 server time.
CRON_SCHEDULE="0 7 1 * *"
CRON_CMD="$CRON_SCHEDULE $SCRIPT_DIR/run.sh"

echo ""
echo "==> Installing cron job..."
(crontab -l 2>/dev/null | grep -qF "$SCRIPT_DIR/run.sh") \
    && echo "    Cron job already exists, skipping." \
    || (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -

echo "    Installed: $CRON_SCHEDULE (1st of each month at 07:00)"
echo ""
echo "Verify with:  crontab -l"
echo "Watch logs:   tail -f $SCRIPT_DIR/logs/sync.log"
