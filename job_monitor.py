#!/usr/bin/env python3
"""
Job Posting Monitor
===================
Monitors SimplifyJobs GitHub repos for new internship/new-grad postings.
Scrapes the README tables every 30 seconds and emails you when new rows appear.

Setup:
  1. pip install requests
  2. Fill in your email config in the CONFIG section below
  3. (Optional) Set a GitHub personal access token for higher rate limits
  4. Run:  python3 job_monitor.py
"""

import hashlib
import json
import os
import re
import select
import shutil
import smtplib
import sys
import termios
import time
import tty
import logging
import threading
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ──────────────────────────────────────────────────────────────────────
# CONFIG — Fill these in before running
# ──────────────────────────────────────────────────────────────────────

EMAIL_CONFIG = {
    # ── Sender (the account that sends the alert) ──
    "smtp_server": "smtp.gmail.com",          # Gmail default; change for Outlook/Yahoo/etc.
    "smtp_port": 587,                          # 587 for TLS (recommended)
    "sender_email": "your_email@gmail.com",   # The email address that sends alerts
    "sender_password": "your_app_password",   # Gmail → App Password (NOT your login password)

    # ── Recipient ──
    "recipient_email": "recipient@example.com",  # Where to receive alerts
}

# GitHub personal access token (optional but recommended)
# Without it you get 60 requests/hour; with it you get 5,000/hour.
# Create one at: https://github.com/settings/tokens (no scopes needed for public repos)
GITHUB_TOKEN = ""  # e.g. "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# How often to check for new jobs (in seconds)
CHECK_INTERVAL = 1

# How often the display rotates one row (in seconds) — independent of CHECK_INTERVAL
DISPLAY_INTERVAL = 0.15

# How often the display rotates while SPACE is held down (fast-forward)
DISPLAY_INTERVAL_FAST = 0.01

# How long to wait after a tap for the OS's key-repeat to confirm it was actually a hold.
# Needs to comfortably exceed the OS's "delay until repeat" (commonly ~400-700ms) — too short
# and a genuine hold gets treated as tap+release+tap+..., which leaves the pause toggle
# flipped an odd number of times (net effect: holding SPACE just pauses).
SPACE_TAP_CONFIRM_TIMEOUT = 0.85

# Once repeating (held), a gap this large between SPACE chars means the key was released.
# Repeat characters arrive much faster than this once key-repeat kicks in.
SPACE_HELD_RELEASE_TIMEOUT = 0.2

# File to persist state between restarts
STATE_FILE = "monitor_state.json"

# Hour (24h) at which the daily "Closed Jobs" summary email is sent
EOD_EMAIL_HOUR = 23

# ──────────────────────────────────────────────────────────────────────
# PAGES TO MONITOR
# ──────────────────────────────────────────────────────────────────────

TARGETS = [
    {
        "name": "New Grad Positions 2026",
        "raw_url": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
        "web_url": "https://github.com/SimplifyJobs/New-Grad-Positions",
    },
    {
        "name": "Summer 2026 Internships",
        "raw_url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md",
        "web_url": "https://github.com/SimplifyJobs/Summer2026-Internships",
    },
    {
        "name": "Off-Season Internships 2026",
        "raw_url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README-Off-Season.md",
        "web_url": "https://github.com/SimplifyJobs/Summer2026-Internships/blob/dev/README-Off-Season.md",
    },
]

# JSON feed — updated before the README, filtered by role + degree
JSON_TARGETS = [
    {
        "name": "New Grad Positions 2026 (JSON)",
        "json_url": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/refs/heads/dev/.github/scripts/listings.json",
        "web_url": "https://github.com/SimplifyJobs/New-Grad-Positions",
    },
    {
        "name": "Summer 2026 Internships (JSON)",
        "json_url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/refs/heads/dev/.github/scripts/listings.json",
        "web_url": "https://github.com/SimplifyJobs/Summer2026-Internships",
    },
]

# Degree values that qualify (case-insensitive, trailing dot optional)
BACHELOR_DEGREES = {"bachelor's", "b.a", "b.a.", "b.s", "b.s.", "bachelor"}

# Roles containing any of these substrings are excluded everywhere
EXCLUDED_ROLE_KEYWORDS: frozenset[str] = frozenset({
    "data analytics", "data science", "data scientist",
    "data analyst", "data science analyst", "data engineering",
    "data intern", "data management", "innovation technology", "data nerd", "data analyzer"
})

# Full-time (New Grad) positions must be in one of these locations to qualify.
# Internships have no location restriction.
FULLTIME_ALLOWED_LOCATIONS: frozenset[str] = frozenset({
    # Northern California
    "palo alto", "san francisco", "sf", "daly city", "santa clara",
    "san jose", "milpitas", "san mateo", "san carlos", "fremont",
    "cupertino", "mountain view", "sunnyvale", "san rafael", "berkeley",
    "menlo park", "bayshore", "burlingame", "millbrae", "oakland", "pleasanton",
    # New York
    "new york", "nyc", "ny",
})

# Source tags and work types for terminal display
SOURCE_TAG: dict[str, str] = {
    "New Grad Positions 2026":        "NG",
    "New Grad Positions 2026 (JSON)": "NG",
    "Summer 2026 Internships":        "SI",
    "Summer 2026 Internships (JSON)": "SI",
    "Off-Season Internships 2026":    "OFF",
}
WORK_TYPE: dict[str, str] = {
    "New Grad Positions 2026":        "Full-Time",
    "New Grad Positions 2026 (JSON)": "Full-Time",
    "Summer 2026 Internships":        "Internship",
    "Summer 2026 Internships (JSON)": "Internship",
    "Off-Season Internships 2026":    "Internship",
}

# 2-letter abbreviations for the 10 most common internship cities
CITY_ABBREVS: dict[str, str] = {
    "san francisco": "SF",
    "new york":      "NY",
    "seattle":       "SE",
    "boston":        "BO",
    "los angeles":   "LA",
    "washington":    "DC",
    "chicago":       "CH",
    "austin":        "AU",
    "san jose":      "SJ",
    "denver":        "DV",
    "toronto":       "TO",
    "vancouver":     "VA",
    # extras worth having
    "atlanta":       "AT",
    "san diego":     "SD",
    "dallas":        "DA",
    "miami":         "MI",
    "remote":        "RM",
}

# ──────────────────────────────────────────────────────────────────────
# LOGGING  (INFO → job_monitor.log only; WARNING+ → console)
# ──────────────────────────────────────────────────────────────────────

_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")
_fh = logging.FileHandler("job_monitor.log")
_fh.setLevel(logging.INFO)
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler()
_ch.setLevel(logging.WARNING)
_ch.setFormatter(_fmt)

log = logging.getLogger("job_monitor")
log.setLevel(logging.INFO)
log.addHandler(_fh)
log.addHandler(_ch)
log.propagate = False

# ──────────────────────────────────────────────────────────────────────
# CORE LOGIC
# ──────────────────────────────────────────────────────────────────────

# Populated by check_json_for_updates each cycle; read by print_latest_jobs
_listings_cache: dict[str, list[dict]] = {}
# (norm_company, norm_title) pairs locked in any README; updated by check_for_updates
_readme_locked_keys: set[tuple[str, str]] = set()
_display_offset: int  = 0     # rotates the visible window each cycle
_display_paused: bool = False   # toggled by a SPACE tap
_display_speedup: bool = False  # True while SPACE is held down — speeds up the ticker
_space_state: str = "idle"      # "idle" | "pending" (tap, awaiting a possible repeat) | "held"
_space_last_time: float = 0.0   # time.time() of the last SPACE char received
DISPLAY_WINDOW  : int = 60    # how many rows are visible at once
DISPLAY_POOL    : int = 60    # total entries fetched to rotate through


def format_age(date_posted: int) -> str:
    if not date_posted:
        return "—"
    age_secs = max(0, int(time.time()) - date_posted)
    if age_secs < 120:
        m, s = divmod(age_secs, 60)
        return f"Super Fresh ({m}m {s}s)" if m else f"Super Fresh ({s}s)"
    age_mins = age_secs // 60
    if age_mins < 60:
        return f"FRESH ({age_mins}m ago)"
    age_hours = age_secs // 3600
    if age_hours < 24:
        return f"{age_hours}h ago"
    return f"{age_secs // 86400}d ago"


def format_work_type(listing: dict, source_name: str) -> str:
    if "New Grad" in source_name:
        return "Full-Time"
    for term in listing.get("terms", []):
        tl = term.lower()
        if "summer" in tl:   return "Summer Internship"
        if "fall"   in tl:   return "Fall Internship"
        if "winter" in tl:   return "Winter Internship"
        if "spring" in tl:   return "Spring Internship"
    return "Internship"


def load_state() -> dict:
    """Load previously seen rows from disk."""
    if Path(STATE_FILE).exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"State file corrupt, resetting: {e}")
            Path(STATE_FILE).unlink(missing_ok=True)
    return {}


def save_state(state: dict):
    """Persist state to disk."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_readme(url: str) -> str:
    """Fetch raw markdown from GitHub."""
    headers = {"Accept": "text/plain"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.text


def fetch_json_listings(url: str) -> list[dict]:
    """Fetch the listings JSON feed from GitHub."""
    headers = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def is_fulltime_location_allowed(locations: list[str]) -> bool:
    """Return True if at least one location is in the NorCal/NYC allowed set."""
    for loc in locations:
        loc_lower = loc.lower()
        if any(city in loc_lower for city in FULLTIME_ALLOWED_LOCATIONS):
            return True
    return False


def is_excluded_role(title: str, category: str = "") -> bool:
    """Return True if the role title or category matches an excluded keyword."""
    combined = (title + " " + category).lower()
    return any(kw in combined for kw in EXCLUDED_ROLE_KEYWORDS)


_LANG_KEYWORDS: frozenset[str] = frozenset({
    "rust", "python", "java", "javascript", "typescript",
    "golang", "kotlin", "swift", "ruby", "scala", "haskell",
    "perl", "php", "lua", "elixir", "clojure",
})

def role_marker(title: str, category: str = "") -> str:
    """Return a '+'-joined marker of SW / HW / RB / AI / FD tags detected in title/category."""
    raw = (title + " " + category).lower()
    c   = " " + re.sub(r"[^a-z0-9]+", " ", raw) + " "
    is_fd = "forward deployed"  in c
    is_lang = (
        any(f" {lang} " in c for lang in _LANG_KEYWORDS)
        or "c++" in raw or "c#" in raw
    )
    is_sw = "software" in c or is_fd or is_lang
    is_hw = "hardware" in c
    is_rb = (
        "robotics"      in c or "autonomous"  in c
        or "isaac"      in c or "mujoco"      in c
        or "perception" in c or "grasping"    in c
        or " vision "   in c or "robot"       in c
    )
    is_ai = (
        "artificial intelligence" in c or " ai "           in c
        or "machine learning"     in c or "llm"            in c
        or "generative"           in c or "ai engineering" in c
        or "ai engineer"          in c
    )
    parts = []
    if is_sw: parts.append("SW")
    if is_hw: parts.append("HW")
    if is_rb: parts.append("RB")
    if is_ai: parts.append("AI")
    if is_fd: parts.append("FD")
    return "+".join(parts) if parts else raw   


def is_qualifying_listing(listing: dict, source_name: str = "") -> bool:
    """Return True if a JSON listing meets all alert criteria."""
    if not listing.get("active", False):
        return False

    degrees = [d.lower().strip() for d in listing.get("degrees", [])]
    if not any(d in BACHELOR_DEGREES for d in degrees):
        return False

    text = " ".join(
        str(v) for v in listing.values() if isinstance(v, str)
    ).lower()
    if "software" not in text and "robotics" not in text:
        return False

    if is_excluded_role(listing.get("title", ""), listing.get("category", "")):
        return False

    # Full-time (New Grad) positions must be in NorCal or NYC
    if "New Grad" in source_name:
        if not is_fulltime_location_allowed(listing.get("locations", [])):
            return False

    return True


def abbrev_location(loc: str) -> str:
    """
    Return a 2-letter uppercase abbreviation for a location string.
    Priority: CITY_ABBREVS match → US state code from 'City, ST' → '_'.
    """
    low = loc.lower().strip()
    for city, code in CITY_ABBREVS.items():
        if city in low:
            return code
    # Try to extract a 2-letter state code: "City, ST" or "City, ST, Country"
    parts = [p.strip() for p in loc.split(",")]
    for part in parts[1:]:
        if len(part) == 2 and part.isalpha():
            return part.upper()
    return "_"


def normalize_for_match(text: str) -> str:
    """Strip markdown links, emoji, and extra whitespace, then lowercase."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) → label
    text = re.sub(r"[^\x00-\x7F]+", "", text)              # remove non-ASCII (emoji)
    return " ".join(text.lower().split())


def parse_table_rows(markdown: str) -> list[dict]:
    """
    Extract job posting rows from the HTML tables in the README.
    Columns: Company | Role | Location | Application | Age
    Returns a list of dicts with a unique fingerprint for each row.
    """
    rows = []
    tag_re = re.compile(r"<[^>]+>", re.DOTALL)
    td_re = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
    tr_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    href_re = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)

    def inner_text(html):
        html = re.sub(r"</?\s*br\s*/?>", " / ", html, flags=re.IGNORECASE)
        return tag_re.sub("", html).strip()

    for tr_match in tr_re.finditer(markdown):
        cells = td_re.findall(tr_match.group(1))
        if len(cells) < 2:
            continue

        company = inner_text(cells[0])
        role = inner_text(cells[1])
        location = inner_text(cells[2]) if len(cells) > 2 else ""

        app_cell = cells[3] if len(cells) > 3 else ""
        app_text = inner_text(app_cell)
        href_m = href_re.search(app_cell)
        apply_url = href_m.group(1) if href_m else ""

        if not company or not role:
            continue

        row = {
            "company": company,
            "role": role,
            "location": location,
            "application": f"[Apply]({apply_url})" if apply_url else app_text,
        }
        fingerprint = hashlib.md5(f"{company}|{role}|{location}".encode()).hexdigest()
        row["_fingerprint"] = fingerprint
        rows.append(row)

    return rows


def strip_markdown_links(text: str) -> str:
    """Convert [text](url) to just text for cleaner display."""
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)


def extract_url(text: str) -> str:
    """Pull the first URL out of a markdown link."""
    m = re.search(r"\[([^\]]*)\]\(([^)]+)\)", text)
    if m:
        return m.group(2)
    m = re.search(r"(https?://\S+)", text)
    return m.group(1) if m else ""


def build_email_html(new_postings: dict[str, list[dict]]) -> str:
    """Build a nice HTML email body showing new postings grouped by source."""
    # Build a name → {web, raw} lookup; always use listings.json as the raw link.
    # Match by prefix so Off-Season (blob URL) resolves to the Summer repo JSON.
    def _find_json_url(web_url: str) -> str:
        for jt in JSON_TARGETS:
            if web_url.startswith(jt["web_url"]):
                return jt["json_url"]
        return ""

    source_urls: dict[str, dict] = {}
    for t in TARGETS:
        source_urls[t["name"]] = {
            "web": t.get("web_url", ""),
            "raw": _find_json_url(t.get("web_url", "")),
        }
    for t in JSON_TARGETS:
        source_urls[t["name"]] = {
            "web": t.get("web_url", ""),
            "raw": t.get("json_url", ""),
        }

    quick_links = [
        ("New Grad Positions",       "https://github.com/SimplifyJobs/New-Grad-Positions"),
        ("Summer 2026 Internships",  "https://github.com/SimplifyJobs/Summer2026-Internships"),
        ("Off-Season Internships",   "https://github.com/SimplifyJobs/Summer2026-Internships/blob/dev/README-Off-Season.md"),
    ]

    html_parts = [
        "<html><body>",
        "<h2 style='color:#2563eb;'>🚀 New Job Postings Detected!</h2>",
        f"<p style='color:#666;'>Found at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>",
        "<p style='font-size:13px; margin-bottom:16px;'>"
        + "  ·  ".join(
            f"<a href='{url}' style='color:#2563eb;text-decoration:none;'>{label}</a>"
            for label, url in quick_links
        )
        + "</p>",
    ]

    for source_name, rows in new_postings.items():
        html_parts.append(f"<h3 style='margin-top:20px;'>{source_name} — {len(rows)} new posting(s)</h3>")
        urls = source_urls.get(source_name, {})
        if urls.get("web") or urls.get("raw"):
            link_parts = []
            if urls.get("web"):
                link_parts.append(
                    f"<a href='{urls['web']}' style='color:#2563eb;margin-right:16px;'>"
                    f"GitHub Repo</a>"
                )
            if urls.get("raw"):
                link_parts.append(
                    f"<a href='{urls['raw']}' style='color:#6b7280;'>"
                    f"Raw Feed</a>"
                )
            html_parts.append(
                f"<p style='margin:4px 0 12px; font-size:13px;'>{'  ·  '.join(link_parts)}</p>"
            )
        html_parts.append(
            "<table border='1' cellpadding='8' cellspacing='0' "
            "style='border-collapse:collapse; width:100%; font-size:14px;'>"
        )
        # Header
        if rows:
            display_keys = [k for k in rows[0] if not k.startswith("_")]
            html_parts.append("<tr style='background:#2563eb; color:white;'>")
            for key in display_keys:
                html_parts.append(f"<th>{key.title()}</th>")
            html_parts.append("<th>Posted At</th>")
            html_parts.append("</tr>")

            for i, row in enumerate(rows):
                bg = "#f8fafc" if i % 2 == 0 else "#ffffff"
                html_parts.append(f"<tr style='background:{bg};'>")
                for key in display_keys:
                    val = row.get(key, "")
                    url = extract_url(val)
                    clean = strip_markdown_links(val)
                    if key == "application" and url:
                        html_parts.append(
                            f"<td style='text-align:center;'>"
                            f"<a href='{url}' style='background:#2563eb;color:white;"
                            f"padding:4px 12px;border-radius:4px;text-decoration:none;"
                            f"font-weight:bold;'>Apply →</a></td>"
                        )
                    elif key == "application":
                        html_parts.append(
                            "<td style='text-align:center;color:#999;font-style:italic;'>"
                            "Job link unavailable</td>"
                        )
                    elif url:
                        html_parts.append(f"<td><a href='{url}'>{clean}</a></td>")
                    else:
                        html_parts.append(f"<td>{clean}</td>")
                html_parts.append(f"<td style='white-space:nowrap;'>{row.get('_detected_at', '—')}</td>")
                html_parts.append("</tr>")

        html_parts.append("</table>")

    html_parts.append(
        "<p style='color:#999; margin-top:20px; font-size:12px;'>"
        "Sent by Job Monitor Script</p>"
    )
    html_parts.append("</body></html>")
    return "\n".join(html_parts)


def send_email(subject: str, html_body: str):
    """Send an HTML email via SMTP."""
    cfg = EMAIL_CONFIG
    msg = MIMEMultipart("alternative")
    msg["From"] = cfg["sender_email"]
    msg["To"] = cfg["recipient_email"]
    msg["Subject"] = subject

    # Plain text fallback
    plain = re.sub(r"<[^>]+>", "", html_body)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg["sender_email"], cfg["sender_password"])
            server.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        log.info("✉️  Email sent successfully!")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


def check_for_updates(state: dict, active_index: dict) -> dict:
    """
    Check all targets for new postings.
    - Qualifying new rows (no 🔒, no 🎓, active in JSON index) → immediate email.
    - Filtered/closed rows → appended to state["pending_closed_jobs"] for EOD email.
    - Rows that were open last cycle and are now 🔒 → also appended to pending.
    active_index: {"by_url": {url→bool}, "by_title": {(company,title)→bool}}
    Returns a dict of {source_name: [new_rows]} for any that have updates.
    """
    global _readme_locked_keys
    by_url   = active_index.get("by_url", {})
    by_title = active_index.get("by_title", {})
    all_new = {}
    new_locked_keys: set[tuple[str, str]] = set()

    for target in TARGETS:
        name = target["name"]
        open_key   = f"readme_open:{name}"
        is_fulltime = "New Grad" in name
        log.info(f"Checking: {name}")

        try:
            markdown = fetch_readme(target["raw_url"])
        except requests.RequestException as e:
            log.warning(f"  ⚠  Failed to fetch {name}: {e}")
            continue

        rows = parse_table_rows(markdown)

        # Rebuild locked-key set from every 🔒 row across all READMEs
        for r in rows:
            if "🔒" in r.get("application", "") and "↳" not in r.get("company", ""):
                new_locked_keys.add((
                    normalize_for_match(r.get("company", "")),
                    normalize_for_match(r.get("role", "")),
                ))

        known = set(state.get(name, []))
        open_fps = set(state.get(open_key, []))

        current_fps = {r["_fingerprint"] for r in rows}
        current_open_fps = {
            r["_fingerprint"] for r in rows if "🔒" not in r.get("application", "")
        }

        if not known:
            log.info(f"  📋 Initial load: {len(rows)} existing rows stored")
            state[name] = list(current_fps)
            state[open_key] = list(current_open_fps)
            continue

        detected_at = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        pending: list = state.setdefault("pending_closed_jobs", [])

        # Postings that were open last cycle and are now locked
        newly_closed_fps = open_fps & {
            r["_fingerprint"] for r in rows if "🔒" in r.get("application", "")
        }
        for r in rows:
            if r["_fingerprint"] in newly_closed_fps:
                r["_detected_at"] = detected_at
                r["reason"] = "🔒 Application closed"
                pending.append(r)
        if newly_closed_fps:
            log.info(f"  🔒 {len(newly_closed_fps)} posting(s) newly closed → EOD queue")

        # New postings this cycle
        qualifying, filtered = [], []
        for r in rows:
            if r["_fingerprint"] in known:
                continue
            r["_detected_at"] = detected_at
            if "🔒" in r.get("application", ""):
                r["reason"] = "🔒 Application already closed"
                filtered.append(r)
            elif "🎓" in r.get("role", ""):
                r["reason"] = "🎓 Advanced degree required"
                filtered.append(r)
            else:
                apply_url    = extract_url(r.get("application", ""))
                url_active   = by_url.get(apply_url) if apply_url else None
                title_active = by_title.get(normalize_for_match(r.get("role", "")))
                if url_active is False or title_active is False:
                    r["reason"] = "🔒 Inactive (verified via JSON)"
                    filtered.append(r)
                elif is_excluded_role(r.get("role", "")):
                    r["reason"] = "🚫 Excluded role type"
                    filtered.append(r)
                elif role_marker(r.get("role", "")) == "?":
                    r["reason"] = "🚫 Not software/robotics/AI"
                    filtered.append(r)
                elif is_fulltime and not is_fulltime_location_allowed(
                        [r.get("location", "")]):
                    r["reason"] = "📍 Outside allowed locations"
                    filtered.append(r)
                else:
                    qualifying.append(r)

        pending.extend(filtered)
        if filtered:
            log.info(f"  🚫 {len(filtered)} new posting(s) filtered → EOD queue")

        if qualifying:
            log.info(f"  🆕 {len(qualifying)} new posting(s) found!")
            all_new[name] = qualifying
        elif not newly_closed_fps and not filtered:
            log.info(f"  ✓  No new postings (tracking {len(rows)} rows)")

        state[name] = list(current_fps)
        state[open_key] = list(current_open_fps)

    _readme_locked_keys = new_locked_keys
    return all_new


def check_json_for_updates(state: dict) -> tuple[dict, dict[str, bool]]:
    """
    Check JSON feed targets for new or updated qualifying listings.
    State keys per target:
      'json:<name>'         → {id: date_updated}  (all listings)
      'json_max_date:<name>'→ highest date_posted seen (int)
      'json_active:<name>'  → [ids] of listings last seen as active+qualifying
    Newly closed qualifying listings → state["pending_closed_jobs"] for EOD email.
    Returns (all_new, active_url_index) where active_url_index maps
    apply_url → is_active for every listing seen across all JSON feeds.
    """
    all_new = {}
    by_url:   dict[str, bool] = {}
    by_title: dict[str, bool] = {}

    for target in JSON_TARGETS:
        name = target["name"]
        state_key = f"json:{name}"
        date_key = f"json_max_date:{name}"
        active_key = f"json_active:{name}"
        log.info(f"Checking JSON: {name}")

        try:
            listings = fetch_json_listings(target["json_url"])
        except requests.RequestException as e:
            log.warning(f"  ⚠  Failed to fetch {name}: {e}")
            continue

        known = state.get(state_key, {})
        max_date_posted = state.get(date_key, 0)
        active_set = set(state.get(active_key, []))
        pending: list = state.setdefault("pending_closed_jobs", [])

        new_rows = []
        current = {}
        new_active_set: set[str] = set()
        detected_at = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        newly_closed_count = 0

        for listing in listings:
            lid = listing.get("id", "")
            date_updated = listing.get("date_updated", 0)
            date_posted = listing.get("date_posted", 0)
            is_active = listing.get("active", False)
            current[lid] = date_updated

            # Build cross-reference indexes for README scraper
            if listing.get("url"):
                by_url[listing["url"]] = is_active
            by_title[normalize_for_match(listing.get("title", ""))] = is_active

            # If the README shows this listing as locked, skip it entirely —
            # no immediate email, no EOD email, regardless of any update.
            lock_key = (
                normalize_for_match(listing.get("company_name", "")),
                normalize_for_match(listing.get("title", "")),
            )
            if lock_key in _readme_locked_keys:
                continue

            # Track active+qualifying set for next cycle's closure detection
            if is_active and is_qualifying_listing(listing, name):
                new_active_set.add(lid)

            # Detect listing that was qualifying+active last cycle, now inactive
            if lid in active_set and not is_active:
                locations = ", ".join(listing.get("locations", []))
                app = f"[Apply]({listing['url']})" if listing.get("url") else "Job link unavailable"
                pending.append({
                    "company": listing.get("company_name", "?"),
                    "role": listing.get("title", "?"),
                    "location": locations,
                    "application": app,
                    "reason": "🔒 Application closed",
                    "_fingerprint": lid,
                    "_detected_at": detected_at,
                    "_locations_list": listing.get("locations", []),
                })
                newly_closed_count += 1
                continue

            is_new = lid not in known
            is_updated = not is_new and known[lid] != date_updated
            is_recent = date_posted > max_date_posted if is_new else True

            if (is_new and is_recent or is_updated) and is_qualifying_listing(listing, name):
                locations = ", ".join(listing.get("locations", []))
                app = f"[Apply]({listing['url']})" if listing.get("url") else "Job link unavailable"
                new_rows.append({
                    "company": listing.get("company_name", "?"),
                    "role": listing.get("title", "?"),
                    "location": locations,
                    "application": app,
                    "_fingerprint": lid,
                    "_detected_at": detected_at,
                    "_locations_list": listing.get("locations", []),
                })

        new_max = max((l.get("date_posted", 0) for l in listings), default=0)
        state[date_key] = max(max_date_posted, new_max)

        if not known:
            log.info(f"  📋 Initial load: {len(listings)} listings indexed")
        else:
            if new_rows:
                log.info(f"  🆕 {len(new_rows)} qualifying listing(s) found!")
                all_new[name] = new_rows
            if newly_closed_count:
                log.info(f"  🔒 {newly_closed_count} listing(s) newly closed → EOD queue")
            if not new_rows and not newly_closed_count:
                log.info(f"  ✓  No new qualifying listings (tracking {len(listings)} total)")

        state[state_key] = current
        state[active_key] = list(new_active_set)
        _listings_cache[name] = listings

    return all_new, {"by_url": by_url, "by_title": by_title}


def _build_entries() -> list[dict]:
    """
    Collect active listings from the JSON cache, sorted newest→oldest.
    Rank is assigned here (1 = newest) and reflects the live sort each tick,
    so it updates automatically when new jobs arrive or ages shift the order.
    """
    entries = []
    for target in JSON_TARGETS:
        name = target["name"]
        is_fulltime = "New Grad" in name
        for listing in _listings_cache.get(name, []):
            if not listing.get("active"):
                continue
            if (normalize_for_match(listing.get("company_name", "")),
                    normalize_for_match(listing.get("title", ""))) in _readme_locked_keys:
                continue
            if is_fulltime and not is_fulltime_location_allowed(listing.get("locations", [])):
                continue
            degrees = [d.lower().strip() for d in listing.get("degrees", [])]
            if degrees and not any(d in BACHELOR_DEGREES for d in degrees):
                continue
            if is_excluded_role(listing.get("title", ""), listing.get("category", "")):
                continue
            all_locs = listing.get("locations", ["?"])
            if is_fulltime:
                display_loc = next(
                    (loc for loc in all_locs if is_fulltime_location_allowed([loc])),
                    all_locs[0],
                )
            else:
                display_loc = all_locs[0]
            entries.append({
                "company":      listing.get("company_name", "?"),
                "work_type":    format_work_type(listing, name),
                "location":     display_loc,
                "marker":       role_marker(listing.get("title", ""), listing.get("category", "")),
                "url":          listing.get("url", ""),
                "_date_posted": listing.get("date_posted", 0),
            })
    entries.sort(key=lambda e: e["_date_posted"], reverse=True)
    pool = entries[:DISPLAY_POOL]
    for i, e in enumerate(pool):
        e["rank"] = i + 1          # 1 = newest, DISPLAY_POOL = oldest
    return pool


def print_latest_jobs():
    """Redraw the terminal in-place with a rotating window of active listings."""
    global _display_offset
    entries = _build_entries()     # ranks and ages recomputed every tick
    if not entries:
        return

    rows  = min(DISPLAY_WINDOW, shutil.get_terminal_size().lines - 2, len(entries))
    n     = len(entries)
    window = [entries[(_display_offset + i) % n] for i in range(rows)]
    if not _display_paused:
        _display_offset = (_display_offset + 1) % n

    rank_w = len(str(DISPLAY_POOL))
    cols   = shutil.get_terminal_size().columns
    RESET  = "\033[0m"
    GREY   = "\033[90m"
    print("\033[H\033[2J", end="", flush=True)
    for e in window:
        age_secs = max(0, int(time.time()) - e["_date_posted"]) if e["_date_posted"] else 0
        if age_secs < 120:
            color = "\033[92m"
        elif age_secs < 3600:
            color = "\033[32m"
        elif age_secs < 7200:
            color = "\033[33m"
        else:
            color = "\033[31m"

        company   = e["company"][:21].ljust(21)
        work_type = e["work_type"][:19].ljust(19)
        location  = e["location"][:25].ljust(25)
        marker    = e.get("marker", "?")[:8].ljust(8)
        age       = format_age(e["_date_posted"])
        rank      = str(e["rank"]).rjust(rank_w)
        link      = e.get("url") or "Link unavailable"

        prefix_visible = f"  {company}  {work_type}  {location}  {marker}  {age}  {rank}  "
        max_link_len   = max(10, cols - len(prefix_visible) - 1)
        link           = link[:max_link_len]
        print(f"{color}{prefix_visible}{link}{RESET}")

    if _display_paused:
        print(f"\n{GREY}  ⏸  PAUSED — press SPACE to resume{RESET}", flush=True)
    else:
        print(flush=True)


def send_eod_email(pending_jobs: list[dict]):
    """Send the daily Closed Jobs summary email."""
    html = build_email_html({"Closed & Filtered Jobs": pending_jobs})
    send_email("🔒 Closed Jobs — Daily Summary", html)
    log.info(f"🔒 EOD email sent — {len(pending_jobs)} closed/filtered job(s).")


def main():
    """Main loop — check every CHECK_INTERVAL seconds."""
    log.info("=" * 60)
    log.info("  Job Posting Monitor — Starting Up")
    log.info("=" * 60)
    log.info(f"  Checking {len(TARGETS)} sources every {CHECK_INTERVAL}s")
    log.info(f"  Alerts will be sent to: {EMAIL_CONFIG['recipient_email']}")
    if GITHUB_TOKEN:
        log.info("  GitHub token: configured ✓")
    else:
        log.info("  GitHub token: NOT SET (rate limit = 60 req/hr)")
        log.info("  Tip: Set GITHUB_TOKEN for 5,000 req/hr")
    log.info("=" * 60)

    state = load_state()

    while True:
        try:
            json_new, active_index = check_json_for_updates(state)
            new_postings = check_for_updates(state, active_index)
            new_postings.update(json_new)
            save_state(state)

            if new_postings:
                total = sum(len(v) for v in new_postings.values())
                seen_abbrevs: list[str] = []
                for rows in new_postings.values():
                    for row in rows:
                        raw_locs = row.get("_locations_list") or [row.get("location", "")]
                        for loc in raw_locs:
                            ab = abbrev_location(loc)
                            if ab and ab not in seen_abbrevs:
                                seen_abbrevs.append(ab)
                loc_str = " · ".join(seen_abbrevs[:6])
                if len(seen_abbrevs) > 6:
                    loc_str += f" +{len(seen_abbrevs) - 6}"
                subject = f"🚀 {total} New Job(s) — {loc_str}" if loc_str else f"🚀 {total} New Job Posting(s) Detected!"
                html = build_email_html(new_postings)
                send_email(subject, html)
            else:
                log.info("No new postings across all sources.")

            # End-of-day closed jobs summary
            now = datetime.now()
            last_eod = state.get("last_eod_email_date", "")
            if now.hour >= EOD_EMAIL_HOUR and last_eod != now.strftime("%Y-%m-%d"):
                pending = state.get("pending_closed_jobs", [])
                if pending:
                    send_eod_email(pending)
                    state["pending_closed_jobs"] = []
                state["last_eod_email_date"] = now.strftime("%Y-%m-%d")
                save_state(state)

        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)

        time.sleep(CHECK_INTERVAL)


def _display_thread_fn():
    """Rotate the terminal display independently of the network check cycle."""
    while True:
        print_latest_jobs()
        time.sleep(DISPLAY_INTERVAL_FAST if _display_speedup else DISPLAY_INTERVAL)


def _keyboard_listener_fn():
    """Listen for SPACE keypresses. A quick tap toggles pause; holding the key down
    (detected via the terminal's OS-level key-repeat) speeds up the ticker instead —
    the tap's pause toggle is reverted once a repeat confirms it's a hold, so pause
    state is left untouched by holding. Runs in a daemon thread."""
    global _display_paused, _display_speedup, _space_state, _space_last_time
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)          # single-char reads; ctrl-c still works
        while True:
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1)
                if ch == " ":
                    _space_last_time = time.time()
                    if _space_state == "idle":
                        _display_paused = not _display_paused   # tap
                        _space_state = "pending"
                    elif _space_state == "pending":
                        _display_paused = not _display_paused   # repeat arrived — revert, it's a hold
                        _space_state = "held"
                        _display_speedup = True
                    # already "held": just keep going, _space_last_time already refreshed above

            now = time.time()
            if _space_state == "pending" and (now - _space_last_time) > SPACE_TAP_CONFIRM_TIMEOUT:
                _space_state = "idle"   # no repeat ever came — it really was just a tap
            elif _space_state == "held" and (now - _space_last_time) > SPACE_HELD_RELEASE_TIMEOUT:
                _space_state = "idle"
                _display_speedup = False
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    threading.Thread(target=_display_thread_fn,    daemon=True).start()
    threading.Thread(target=_keyboard_listener_fn, daemon=True).start()
    main()
