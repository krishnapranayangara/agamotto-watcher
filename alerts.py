"""Save subscribers and email them when a new position opens."""

import json
import os
import smtplib
import threading
from email.message import EmailMessage
from pathlib import Path

STORE = Path(__file__).resolve().parent / "subscribers.json"
LOCK = threading.Lock()
FROM = "perspectivecomputing@gmail.com"


def load():
    if not STORE.exists():
        return {"emails": [], "alerted": []}
    try:
        data = json.loads(STORE.read_text())
    except json.JSONDecodeError:
        return {"emails": [], "alerted": []}
    data.setdefault("emails", [])
    data.setdefault("alerted", [])
    return data


def save(data):
    STORE.write_text(json.dumps(data, indent=2))


def add_subscriber(email):
    address = email.strip().lower()
    with LOCK:
        data = load()
        if address not in data["emails"]:
            data["emails"].append(address)
            save(data)
    return address


def mark_seen(jobs):
    with LOCK:
        data = load()
        known = set(data["alerted"])
        for job in jobs:
            if job["id"] not in known:
                data["alerted"].append(job["id"])
                known.add(job["id"])
        save(data)


def mail_ready():
    return bool(os.environ.get("GMAIL_APP_PASSWORD"))


def send_alert(jobs):
    with LOCK:
        data = load()
        pending = [job for job in jobs if job["id"] not in set(data["alerted"])]
        emails = list(data["emails"])
    if not pending or not emails or not mail_ready():
        return 0
    sent = 0
    for address in emails:
        if deliver(address, pending):
            sent += 1
    if sent:
        mark_seen(pending)
    return sent


def deliver(address, jobs):
    lines = []
    for job in jobs:
        lines.append(f"{job['title']}\n{job['client']} — {job['location']}\n{job['url']}\n")
    message = EmailMessage()
    message["From"] = FROM
    message["To"] = address
    message["Subject"] = "New position opened" if len(jobs) == 1 else f"{len(jobs)} new positions opened"
    message.set_content(
        "A new position opened.\n\n" + "\n".join(lines) + "\nSent from the Jobs watcher."
    )
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(FROM, os.environ["GMAIL_APP_PASSWORD"])
            smtp.send_message(message)
        return True
    except Exception:
        return False
