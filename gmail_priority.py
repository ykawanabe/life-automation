#!/usr/bin/env python3
"""
gmail_priority.py

Fetches unread Gmail messages from the last 24 hours, scores each one
priority 1-5 using Claude, and posts the top results (>= 4) to Slack.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = Path("token.json")
CREDENTIALS_PATH = Path("credentials.json")

PRIORITY_EMOJI = {1: "⚪", 2: "🟢", 3: "🔵", 4: "🟠", 5: "🔴"}


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service():
    """Return an authenticated Gmail API service, refreshing/creating OAuth tokens as needed."""
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                sys.exit(
                    "ERROR: credentials.json not found.\n"
                    "Download it from the Google Cloud Console "
                    "(APIs & Services → Credentials → OAuth 2.0 Client IDs)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_unread_emails(service):
    """Return a list of dicts with subject, sender, and snippet for unread emails in the last 24 h."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    query = f"is:unread after:{since.strftime('%Y/%m/%d')}"

    result = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    message_stubs = result.get("messages", [])

    emails = []
    for stub in message_stubs:
        msg = service.users().messages().get(
            userId="me",
            id=stub["id"],
            format="metadata",
            metadataHeaders=["Subject", "From"],
        ).execute()

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        emails.append({
            "id": stub["id"],
            "subject": headers.get("Subject", "(no subject)"),
            "sender": headers.get("From", "(unknown)"),
            "snippet": msg.get("snippet", ""),
        })

    return emails


# ── Claude ────────────────────────────────────────────────────────────────────

def score_emails(emails):
    """
    Send all emails to Claude in one call and return a list of
    {"priority": int, "reason": str} dicts, one per email (same order).
    """
    client = anthropic.Anthropic()

    numbered = "\n\n".join(
        f"[{i + 1}]\nFrom: {e['sender']}\nSubject: {e['subject']}\nSnippet: {e['snippet']}"
        for i, e in enumerate(emails)
    )

    prompt = f"""\
You are an email triage assistant. Score each email's priority on a 1–5 scale and give a short reason.

Scale:
  5 – Urgent: requires immediate action (e.g. outages, deadlines, security alerts)
  4 – High: needs attention today (e.g. requests from managers, important clients)
  3 – Normal: routine business communication
  2 – Low: can wait a few days
  1 – Minimal: newsletters, automated notifications, marketing

Emails:
{numbered}

Return ONLY a valid JSON array with exactly {len(emails)} objects in the same order, no other text:
[{{"priority": <1-5>, "reason": "<one concise sentence>"}}]"""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )

    # Skip thinking blocks; grab the text block
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text.strip())


# ── Slack ─────────────────────────────────────────────────────────────────────

def post_to_slack(webhook_url, email, score):
    """Post a single scored email to the Slack channel."""
    emoji = PRIORITY_EMOJI.get(score["priority"], "⚪")
    text = (
        f"{emoji} *[{score['priority']}]* "
        f"*{email['sender']}* — {email['subject']}\n"
        f"_{score['reason']}_"
    )
    resp = requests.post(webhook_url, json={"text": text}, timeout=10)
    resp.raise_for_status()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        sys.exit("ERROR: SLACK_WEBHOOK_URL environment variable is not set.")

    print("Authenticating with Gmail…")
    service = get_gmail_service()

    print("Fetching unread emails from the last 24 hours…")
    emails = fetch_unread_emails(service)
    print(f"  Found {len(emails)} unread email(s).")

    if not emails:
        print("Nothing to process.")
        return

    print("Scoring with Claude…")
    scores = score_emails(emails)

    high_priority = [
        (emails[i], scores[i])
        for i in range(len(emails))
        if scores[i]["priority"] >= 4
    ]
    print(f"  {len(high_priority)} email(s) with priority ≥ 4.")

    if not high_priority:
        print("No high-priority emails — nothing posted to Slack.")
        return

    print("Posting to Slack…")
    for email, score in high_priority:
        post_to_slack(webhook_url, email, score)
        print(f"  [{score['priority']}] {email['subject'][:60]}")

    print("Done.")


if __name__ == "__main__":
    main()
