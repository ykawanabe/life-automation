#!/usr/bin/env python3
"""
gmail_priority/main.py

Fetches unread Gmail messages from the last 24 hours, scores each one
priority 1-5 using Claude, and posts a full prioritized digest to Slack.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Resolve paths relative to this file so the script works from any CWD
HERE = Path(__file__).parent
load_dotenv(HERE.parent / ".env")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = HERE / "token.json"


def find_credentials_file():
    """
    Return the path to the Google OAuth credentials file.

    Accepts either the canonical name (credentials.json) or the raw filename
    Google generates when you download from the Cloud Console
    (client_secret_<id>.apps.googleusercontent.com.json).
    Both are looked up relative to this script's directory.
    """
    canonical = HERE / "credentials.json"
    if canonical.exists():
        return canonical

    matches = sorted(HERE.glob("client_secret_*.json"))
    if matches:
        return matches[0]

    sys.exit(
        "ERROR: No Google OAuth credentials file found.\n"
        "Download it from the Google Cloud Console "
        "(APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON)\n"
        "and place it in the gmail_priority/ directory."
    )

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
            credentials_path = find_credentials_file()
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_unread_emails(service):
    """Return a list of dicts with id, subject, sender, and snippet for all inbox emails."""
    result = service.users().messages().list(userId="me", q="in:inbox category:primary", maxResults=100).execute()
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


def gmail_link(message_id):
    """Return a Gmail deep link for a given message ID."""
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"


# ── Claude ────────────────────────────────────────────────────────────────────

def score_emails(emails):
    """
    Send all emails to Claude in one call and return a list of
    {"priority": int, "reason": str, "action_needed": bool} dicts, one per email (same order).
    """
    client = anthropic.Anthropic()

    numbered = "\n\n".join(
        f"[{i + 1}]\nFrom: {e['sender']}\nSubject: {e['subject']}\nSnippet: {e['snippet']}"
        for i, e in enumerate(emails)
    )

    prompt = f"""\
You are an email triage assistant. Score each email's priority and summarise it.

Priority scale:
  5 – Urgent: requires immediate action (e.g. outages, deadlines, security alerts)
  4 – High: needs attention today (e.g. requests from managers, important clients)
  3 – Normal: routine business communication
  2 – Low: can wait a few days
  1 – Minimal: newsletters, automated notifications, marketing

Emails:
{numbered}

Return ONLY a valid JSON array with exactly {len(emails)} objects in the same order, no other text:
[{{"priority": <1-5>, "reason": "<one concise sentence summary>", "action_needed": <true|false>}}]"""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # Strip markdown code fences if present (e.g. ```json ... ```)
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    return json.loads(text)


# ── Slack ─────────────────────────────────────────────────────────────────────

def post_digest_to_slack(webhook_url, emails, scores):
    """Post a full prioritized email digest to Slack as a single message."""
    # Pair and sort by priority descending
    paired = sorted(
        zip(emails, scores),
        key=lambda x: x[1]["priority"],
        reverse=True,
    )

    date_str = datetime.now().strftime("%b %d, %Y")
    lines = [f"*📬 Email Digest — {date_str} — {len(emails)} unread*\n"]

    for email, score in paired:
        emoji = PRIORITY_EMOJI.get(score["priority"], "⚪")
        link = gmail_link(email["id"])
        action_tag = " ⚡ *Action needed*" if score["action_needed"] else ""
        lines.append(
            f"{emoji} *[{score['priority']}] {email['subject']}*{action_tag}\n"
            f"From: {email['sender']}\n"
            f"_{score['reason']}_\n"
            f"<{link}|Open in Gmail>"
        )

    text = "\n\n".join(lines)
    resp = requests.post(webhook_url, json={"text": text}, timeout=10)
    resp.raise_for_status()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        sys.exit("ERROR: SLACK_WEBHOOK_URL environment variable is not set.")

    print("Authenticating with Gmail…")
    service = get_gmail_service()

    print("Fetching inbox emails…")
    emails = fetch_unread_emails(service)
    print(f"  Found {len(emails)} unread email(s).")

    if not emails:
        print("Nothing to process.")
        return

    print("Scoring with Claude…")
    scores = score_emails(emails)

    action_count = sum(1 for s in scores if s["action_needed"])
    print(f"  {action_count} email(s) require action.")

    print("Posting digest to Slack…")
    post_digest_to_slack(webhook_url, emails, scores)
    print("Done.")


if __name__ == "__main__":
    main()
