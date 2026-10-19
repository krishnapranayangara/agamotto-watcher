#!/usr/bin/env python3
"""Watch public boards for the configured job families posted in the past week."""

import json
import re
import html
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
WEEK = timedelta(days=7)


def matches_category(category, title, summary=""):
    text = f"{title} {summary}"
    if category == "boomi":
        return re.search(r"\bboomi\b", text, re.I) is not None
    if category == "react-python":
        return bool(
            re.search(r"\bfull[\s-]?stack\b", text, re.I)
            and re.search(r"\breact\b", text, re.I)
            and re.search(r"\bpython\b", text, re.I)
        )
    if category == "react-java":
        return bool(
            re.search(r"\bfull[\s-]?stack\b", text, re.I)
            and re.search(r"\breact\b", text, re.I)
            and (
                re.search(r"\bjava\b", text, re.I)
                or re.search(r"\bspring(?:\s|-)?boot\b", text, re.I)
            )
        )
    if category == "qa-playwright":
        return bool(
            re.search(r"\b(qa|quality|sdet|tester|test|automation)\b", text, re.I)
            and re.search(r"\bplaywright\b", text, re.I)
        )
    return False


def matches_linkedin_category(category, title):
    if category == "boomi":
        return matches_category(category, title)
    if category in {"react-python", "react-java"}:
        return re.search(r"\bfull[\s-]?stack\b", title, re.I) is not None
    if category == "qa-playwright":
        return bool(
            re.search(r"\b(qa|quality|sdet|tester|test|automation|playwright)\b", title, re.I)
        )
    return matches_category(category, title)


def clean(value):
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", text).strip()


def role_kind(title):
    lowered = title.lower()
    if "architect" in lowered:
        return "Architect"
    if "lead" in lowered or "sme" in lowered:
        return "Lead"
    if "consultant" in lowered:
        return "Consultant"
    if re.search(r"\b(qa|sdet|tester|test engineer)\b", lowered):
        return "QA"
    return "Developer"


def format_posted(when):
    return when.astimezone(EASTERN).strftime("%-d %b %Y, %-I:%M %p")


def clip(text, limit=320):
    text = clean(text)
    if len(text) <= limit:
        return text or "No description was published."
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def linkedin_jobs(now):
    found = []
    seen = set()
    cutoff = (now - WEEK).date()
    responses = 0
    for category, query in SEARCHES:
        url = (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?keywords={quote_plus(query)}&f_TPR=r604800&location=United%20States&start=0"
        )
        try:
            page = fetch(url)
        except Exception:
            continue
        responses += 1
        cards = re.findall(
            r'href="(https://www.linkedin.com/jobs/view/[^"?]+).*?'
            r'base-search-card__title">\s*(.*?)\s*</h3>.*?'
            r'base-search-card__subtitle">\s*(.*?)\s*</h4>.*?'
            r'job-search-card__location">\s*(.*?)\s*</span>.*?'
            r'job-search-card__listdate[^>]*datetime="([^"]+)"',
            page,
            re.S,
        )
        for href, title, company, location, posted_on in cards:
            title = clean(title)
            job_id = href.rstrip("/").rsplit("-", 1)[-1]
            if job_id in seen or not matches_linkedin_category(category, title):
                continue
            posted = datetime.fromisoformat(posted_on).replace(tzinfo=EASTERN)
            if posted.date() < cutoff:
                continue
            seen.add(job_id)
            found.append({
                "id": "li-" + job_id,
                "category": category,
                "role": role_kind(title),
                "client": clean(company) or "Company not listed",
                "location": clean(location) or "Location not listed",
                "title": title,
                "pay": "Not listed",
                "posted": format_posted(posted),
                "postedAt": posted.isoformat(),
                "positionType": "Not stated",
                "email": "Not listed",
                "url": href,
                "summary": "Public LinkedIn listing. Pay, position type, and recruiter email were not on the search card.",
            })
    if responses == 0:
        raise RuntimeError("LinkedIn public search did not respond")
    return found


def _dice_search(now, pages, category, query):
    found = []
    seen = set()
    for page in pages:
        url = (
            f"https://www.dice.com/jobs?q={quote_plus(query)}"
            f"&filters.postedDate=SEVEN&page={page}&pageSize=20"
        )
        page_html = fetch(url)
        chunks = []
        for raw in re.findall(r"self\.__next_f\.push\(\[1,(.*?)\]\)</script>", page_html):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, str):
                chunks.append(value)
        joined = "\n".join(chunks)
        marker = joined.find('"jobList":{"data":')
        if marker < 0:
            break
        start = joined.find("[", marker)
        depth = 0
        data = []
        for index, char in enumerate(joined[start:], start):
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    data = json.loads(joined[start : index + 1])
                    break
        if not data:
            break
        for job in data:
            guid = job.get("guid")
            title = clean(job.get("title"))
            summary = clean(job.get("summary"))
            if not guid or guid in seen or not matches_category(category, title, summary):
                continue
            posted = datetime.fromisoformat(job["postedDate"].replace("Z", "+00:00"))
            if now - posted > WEEK:
                continue
            seen.add(guid)
            location = (job.get("jobLocation") or {}).get("displayName") or "Location not listed"
            if job.get("isRemote"):
                location = "Remote"
            workplace = (job.get("workplaceTypes") or [None])[0]
            if workplace and not job.get("isRemote"):
                location = f"{location}, {workplace.replace('-', ' ').lower()}"
            salary = clean(job.get("salary") or "").replace("$$", "$").replace(" - ", "–")
            found.append({
                "id": guid,
                "category": category,
                "role": role_kind(title),
                "client": clean(job.get("companyName")) or "Company not listed",
                "location": location,
                "title": title,
                "pay": salary or "Not listed",
                "posted": format_posted(posted),
                "postedAt": posted.isoformat(),
                "positionType": clean(job.get("employmentType")) or "Not stated",
                "email": "Not listed",
                "url": job.get("detailsPageUrl") or f"https://www.dice.com/job-detail/{guid}",
                "summary": clip(job.get("summary")),
            })
        if len(data) < 20:
            break
    return found


def _dice_pages(now, pages, searches=SEARCHES):
    found = []
    for category, query in searches:
        found.extend(_dice_search(now, pages, category, query))
    return found


def dice_jobs(now):
    return _dice_pages(now, (1, 2, 3))


LOCK = threading.Lock()
CACHE = {
    "updated": "",
    "label": "Watching for jobs…",
    "jobs": [],
    "notes": ["Checking Dice continuously and public LinkedIn listings for the configured job families."],
}
JOBS = {}


def publish(notes):
    now = datetime.now(timezone.utc)
    fresh = []
    for job in list(JOBS.values()):
        posted = datetime.fromisoformat(job["postedAt"])
        if now - posted <= WEEK:
            fresh.append(job)
        else:
            JOBS.pop(job["id"], None)
    fresh.sort(key=lambda job: datetime.fromisoformat(job["postedAt"]), reverse=True)
    local = now.astimezone(EASTERN)
    with LOCK:
        CACHE["jobs"] = fresh
        CACHE["updated"] = local.isoformat(timespec="seconds")
        CACHE["label"] = local.strftime("Updated %-d %b %Y, %-I:%M %p")
        CACHE["notes"] = notes


def remember(jobs):
    added = []
    for job in jobs:
        key = (job["title"].lower(), job["client"].lower())
        existing = JOBS.get(job["id"])
        duplicate = next((item for item in JOBS.values() if (item["title"].lower(), item["client"].lower()) == key), None)
        if duplicate and duplicate["id"] != job["id"]:
            continue
        if existing and existing.get("summary") and job["summary"].startswith("Public LinkedIn"):
            continue
        if job["id"] not in JOBS:
            added.append(job)
        JOBS[job["id"]] = job
    return added


def watch():
    from alerts import mark_seen, send_alert

    last_deep = 0
    search_index = 0
    primed = False
    while True:
        started = time.monotonic()
        now = datetime.now(timezone.utc)
        notes = []
        opened = []
        try:
            current_search = SEARCHES[search_index]
            search_index = (search_index + 1) % len(SEARCHES)
            latest = _dice_pages(now, (1,), (current_search,))
            opened.extend(remember(latest))
            notes.append(
                f"Dice: checked {current_search[1]}, "
                f"{sum(1 for job in JOBS.values() if not str(job['id']).startswith('li-'))} matching jobs this week"
            )
        except Exception as error:
            notes.append(f"Dice: {error.__class__.__name__}")
        if time.monotonic() - last_deep > 30:
            last_deep = time.monotonic()
            try:
                opened.extend(remember(_dice_pages(now, (2, 3))))
            except Exception:
                pass
            try:
                found = remember(linkedin_jobs(now))
                opened.extend(found)
                notes.append(f"LinkedIn public listings: {len(found)} new matching jobs")
            except Exception:
                notes.append("LinkedIn public search did not respond. Signed-in LinkedIn results are not included.")
        if primed:
            send_alert(opened)
        else:
            mark_seen(opened)
            primed = True
        notes.append("Newest posting is first. Pay and emails appear only when a listing states them.")
        publish(notes)
        time.sleep(max(0, 1 - (time.monotonic() - started)))


def snapshot():
    with LOCK:
        return json.loads(json.dumps(CACHE))
