# life-automation

Small tools that automate my life via Slack.

## Tools

- **gmail_priority** — fetches emails from your Gmail Primary inbox, scores them 1–5 with Claude, and posts a prioritised digest to Slack.

## How to run

Set env vars in `.env`:
```
ANTHROPIC_API_KEY=...
SLACK_WEBHOOK_URL=...
```
Place `credentials.json` (Google OAuth 2.0 client secret) in `gmail_priority/`.
```bash
pip install -r requirements.txt
python gmail_priority/main.py
```
