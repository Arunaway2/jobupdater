# Job Posting Monitor — Setup Guide

## What It Does
Monitors these GitHub repos every 30 seconds for new postings:
- [New Grad Positions](https://github.com/SimplifyJobs/New-Grad-Positions)
- [Summer 2026 Internships](https://github.com/SimplifyJobs/Summer2026-Internships)
- [Off-Season Internships](https://github.com/SimplifyJobs/Summer2026-Internships/blob/dev/README-Off-Season.md)

When a new row appears in any of their tables, you get an HTML email with the details.

## Quick Start

### 1. Install Python dependency
```bash
pip install requests
```

### 2. Set up Gmail App Password
You **cannot** use your regular Gmail password. You need an App Password:

1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** if not already on
3. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
4. Select "Mail" and generate a password
5. Copy the 16-character password (e.g., `uvhw qeli nhzq vkyg`)

### 3. Configure the script
Open `job_monitor.py` and fill in:

```python
EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "sender_email": "you@gmail.com",        # Your Gmail
    "sender_password": "abcd efgh ijkl mnop", # App Password from step 2
    "recipient_email": "you@gmail.com",      # Where to get alerts
}
```

### 4. (Recommended) Add a GitHub token
Without a token, GitHub limits you to 60 API requests/hour.  
At 30-second intervals × 3 pages = 360 requests/hour → **you'll get rate-limited.**

Create a free token:
1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. Click **"Generate new token (classic)"**
3. No scopes needed (public repos only)
4. Paste it into the script:

```python
GITHUB_TOKEN = "ghp_your_token_here"
```

### 5. Run it
```bash
python3 job_monitor.py
```

On first run, it loads all existing postings silently (no email spam).  
After that, you only get emailed when **new rows** appear.

## Run in Background

### Linux/Mac (using nohup)
```bash
nohup python3 job_monitor.py > monitor.log 2>&1 &
```

### Linux (using systemd) — survives reboots
Create `/etc/systemd/system/job-monitor.service`:
```ini
[Unit]
Description=Job Posting Monitor
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/job_monitor.py
WorkingDirectory=/path/to/
Restart=always
RestartSec=10
User=your_username

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl enable job-monitor
sudo systemctl start job-monitor
sudo journalctl -u job-monitor -f   # view logs
```

## Using Outlook/Yahoo Instead of Gmail

**Outlook:**
```python
"smtp_server": "smtp.office365.com",
"smtp_port": 587,
```

**Yahoo:**
```python
"smtp_server": "smtp.mail.yahoo.com",
"smtp_port": 587,
```
(Yahoo also requires an App Password from account security settings.)

## Adjusting Check Interval
Edit this line in the script:
```python
CHECK_INTERVAL = 30  # seconds
```

> **Note:** Without a GitHub token, keep this at 60+ seconds to stay under rate limits.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `403` from GitHub | You're rate-limited. Add a `GITHUB_TOKEN`. |
| `SMTPAuthenticationError` | Wrong password. Make sure you're using an **App Password**, not your login password. |
| `Connection refused` on SMTP | Check `smtp_server` and `smtp_port`. Your network/ISP may block port 587. |
| No emails on first run | Normal! First run stores existing data. New emails come only for *new* postings. |
