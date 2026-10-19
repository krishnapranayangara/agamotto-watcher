#!/usr/bin/env python3
"""Four crawlers that keep the past week of matching roles in the job feed."""

import html
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from alerts import mark_seen, send_alert

EASTERN = ZoneInfo("America/New_York")
WEEK = timedelta(days=7)
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
CRAWLERS = (
    {"id": "boomi", "label": "Boomi", "queries": ("Boomi", "Dell Boomi", "Boomi developer", "Boomi architect")},
    {"id": "react-python", "label": "Full stack (React, Python)", "queries": ("full stack React Python", "React Django", "React FastAPI", "React Flask developer")},
    {"id": "react-java", "label": "Full stack (React, Java Spring Boot)", "queries": ("React Spring Boot", "full stack React Java", "React Java developer", "Java Spring Boot React")},
    {"id": "qa-playwright", "label": "QA Test Engineer / Automation (Playwright)", "queries": ("Playwright QA", "Playwright SDET", "Playwright automation", "Playwright test engineer")},
)
SOURCES = ("dice", "linkedin", "simplyhired", "boards")
LOCK = threading.Lock()
WATCHER = threading.Event()
WATCHER.set()
CACHE = {
    "updated": "",
    "label": "Watching for jobs…",
    "jobs": [],
    "notes": ["Four crawlers are checking Dice, LinkedIn, SimplyHired, and the public remote boards."],
}
JOBS = {}
STARTED = time.monotonic()
LAST_DEVIT = 0.0


def searchable(value):
    return re.sub(r"[_/+]+", " ", clean(value))


def matches_category(category, title, summary=""):
    text = searchable(f"{title} {summary}")
    if category == "boomi":
        return re.search(r"\bboomi\b", text, re.I) is not None
    if category == "react-python":
        return re.search(r"\breact(?:\.?js)?\b", text, re.I) is not None and re.search(r"\b(python|django|flask|fastapi)\b", text, re.I) is not None
    if category == "react-java":
        return re.search(r"\breact(?:\.?js)?\b", text, re.I) is not None and (
            re.search(r"\bjava\b", text, re.I) is not None or re.search(r"\bspring(?:\s|-)?boot\b", text, re.I) is not None
        )
    if category == "qa-playwright":
        return re.search(r"\bplaywright\b", text, re.I) is not None and re.search(r"\b(qa|quality|sdet|tester|test|automation)\b", text, re.I) is not None
    if category == "ai-engineer":
        return re.search(r"\b(ai|artificial intelligence|machine learning|llm)\b", text, re.I) is not None and re.search(r"\bengineer\b", title, re.I) is not None
    return False


def classify(title, summary=""):
    if re.search(r"\bboomi\b", title, re.I):
        return "boomi"
    if matches_category("qa-playwright", title, summary):
        return "qa-playwright"
    python = matches_category("react-python", title, summary)
    java = matches_category("react-java", title, summary)
    if python and java:
        if re.search(r"\b(python|django|flask|fastapi)\b", title, re.I):
            return "react-python"
        if re.search(r"\b(java|spring)\b", title, re.I):
            return "react-java"
        return "react-python"
    if python:
        return "react-python"
    if java:
        return "react-java"
    if matches_category("boomi", title, summary):
        return "boomi"
    return ""


def clean(value):
    text = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", " ", value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def role_kind(title):
    lowered = title.lower()
    if "architect" in lowered:
        return "Architect"
    if "lead" in lowered or "sme" in lowered:
        return "Lead"
    if "consultant" in lowered:
        return "Consultant"
    if re.search(r"\b(qa|sdet|tester|test engineer|quality)\b", lowered):
        return "QA"
    return "Developer"


def format_posted(when):
    return when.astimezone(EASTERN).strftime("%-d %b %Y, %-I:%M %p")


def clip(text, limit=320):
    text = clean(text)
    if len(text) <= limit:
        return text or "No description was published."
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def find_email(text):
    match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", clean(text), re.I)
    return match.group(0) if match else "Not listed"


def sponsorship(text):
    return "available" if re.search(r"\bh-?1b\b|visa sponsorship|will sponsor", text, re.I) else "not-stated"


def fetch(url):
    request = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json,application/rss+xml,application/xml,text/xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8", "replace")


def aware(when):
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when


def listing(fields, now):
    posted = aware(fields["posted"]) if isinstance(fields["posted"], datetime) else aware(datetime.fromtimestamp(fields["posted"], timezone.utc) if isinstance(fields["posted"], (int, float)) else datetime.fromisoformat(str(fields["posted"]).replace("Z", "+00:00")))
    if now - posted > WEEK or posted - now > timedelta(days=1):
        return None
    summary = clip(fields.get("summary"))
    if fields.get("source"):
        summary = f"Source: {fields['source']}. {summary}"
    title = clean(fields["title"])
    return {
        "id": str(fields["id"]),
        "category": fields["category"],
        "role": role_kind(title),
        "client": clean(fields.get("client")) or "Company not listed",
        "location": clean(fields.get("location")) or "Location not listed",
        "title": title,
        "pay": clean(fields.get("pay")) or "Not listed",
        "posted": format_posted(posted),
        "postedAt": posted.isoformat(),
        "positionType": clean(fields.get("positionType")) or "Not stated",
        "email": fields.get("email") or find_email(summary),
        "url": fields["url"],
        "summary": summary,
        "h1b": sponsorship(f"{title} {summary}"),
    }


def richness(job):
    if not job:
        return 0
    score = 0
    if job.get("pay") and job["pay"] != "Not listed":
        score += 3
    if job.get("summary") and not job["summary"].startswith(("Public ", "Source:")):
        score += 2
    if len(job.get("summary") or "") > 120:
        score += 1
    return score


def prefer(existing, incoming):
    if existing is None:
        return incoming
    if richness(incoming) <= richness(existing):
        nxt = dict(existing)
        if existing.get("pay") in (None, "", "Not listed") and incoming.get("pay") not in (None, "", "Not listed"):
            nxt["pay"] = incoming["pay"]
        if existing.get("email") == "Not listed" and incoming.get("email") != "Not listed":
            nxt["email"] = incoming["email"]
        return nxt
    merged = dict(incoming)
    merged["id"] = existing["id"]
    merged["category"] = existing["category"]
    merged["posted"] = existing["posted"]
    merged["postedAt"] = existing["postedAt"]
    return merged


def remember(jobs):
    added = []
    with LOCK:
        for job in jobs:
            if job is None:
                continue
            key = (job["title"].lower(), job["client"].lower())
            duplicate = next((item for item in JOBS.values() if (item["title"].lower(), item["client"].lower()) == key and item["id"] != job["id"]), None)
            if duplicate:
                JOBS[duplicate["id"]] = prefer(duplicate, {**job, "id": duplicate["id"]})
                continue
            if job["id"] not in JOBS:
                added.append(job)
            JOBS[job["id"]] = prefer(JOBS.get(job["id"]), job)
    return added


def publish(notes):
    now = datetime.now(timezone.utc)
    with LOCK:
        fresh = []
        for job in list(JOBS.values()):
            posted = datetime.fromisoformat(job["postedAt"])
            if now - aware(posted) <= WEEK:
                fresh.append(job)
            else:
                JOBS.pop(job["id"], None)
        fresh.sort(key=lambda job: datetime.fromisoformat(job["postedAt"]), reverse=True)
        local = now.astimezone(EASTERN)
        CACHE["jobs"] = fresh
        CACHE["updated"] = local.isoformat(timespec="seconds")
        CACHE["label"] = local.strftime("Updated %-d %b %Y, %-I:%M %p")
        CACHE["notes"] = notes


def watcher_running():
    return WATCHER.is_set()


def set_watcher(running):
    if running:
        WATCHER.set()
    else:
        WATCHER.clear()
    return watcher_running()


def snapshot():
    with LOCK:
        payload = json.loads(json.dumps(CACHE))
    payload["running"] = watcher_running()
    return payload


def linkedin_jobs(page, category, now):
    found = []
    seen = set()
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
        if not job_id or job_id in seen or not matches_category(category, title):
            continue
        seen.add(job_id)
        found.append(listing({
            "id": "li-" + job_id,
            "category": category,
            "title": title,
            "client": company,
            "location": location,
            "posted": datetime.fromisoformat(posted_on).replace(tzinfo=timezone.utc),
            "url": href,
            "summary": "Public LinkedIn listing. Pay, position type, and recruiter email were not on the search card.",
        }, now))
    return [job for job in found if job]


def search_linkedin(category, query, start, now):
    url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={quote_plus(query)}&f_TPR=r604800&location=United%20States&start={start}"
    )
    return linkedin_jobs(fetch(url), category, now)


def parse_json_array(joined, start):
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(joined)):
        char = joined[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(joined[start : index + 1])
                except json.JSONDecodeError:
                    return []
    return []


def dice_jobs_from_html(page, category, now):
    chunks = []
    for raw in re.findall(r"self\.__next_f\.push\(\[1,(.*?)\]\)</script>", page, re.S):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, str):
            chunks.append(value)
    joined = "\n".join(chunks)
    marker = joined.find('"jobList":{"data":')
    if marker < 0:
        return []
    data = parse_json_array(joined, joined.find("[", marker))
    found = []
    for job in data:
        title = clean(job.get("title"))
        summary = clean(job.get("summary"))
        guid = job.get("guid")
        if not guid or not matches_category(category, title, summary):
            continue
        posted = datetime.fromisoformat(job["postedDate"].replace("Z", "+00:00"))
        location = (job.get("jobLocation") or {}).get("displayName") or "Location not listed"
        if job.get("isRemote"):
            location = "Remote"
        workplace = (job.get("workplaceTypes") or [None])[0]
        if workplace and not job.get("isRemote"):
            location = f"{location}, {workplace.replace('-', ' ').lower()}"
        found.append(listing({
            "id": guid,
            "category": category,
            "title": title,
            "client": job.get("companyName"),
            "location": location,
            "pay": clean(job.get("salary") or "").replace("$$", "$").replace(" - ", "–"),
            "posted": posted,
            "positionType": job.get("employmentType"),
            "url": job.get("detailsPageUrl") or f"https://www.dice.com/job-detail/{guid}",
            "summary": summary,
            "email": find_email(summary),
        }, now))
    return [job for job in found if job]


def search_dice(category, query, page, now):
    url = f"https://www.dice.com/jobs?q={quote_plus(query)}&filters.postedDate=SEVEN&page={page}&pageSize=20"
    return dice_jobs_from_html(fetch(url), category, now)


def relative_days(label):
    text = clean(label).lower()
    if re.search(r"today|just posted|^new\b", text):
        return 0
    weeks = re.search(r"(\d+)\s*w", text)
    if weeks:
        return int(weeks.group(1)) * 7
    days = re.search(r"(\d+)\s*d", text)
    if days:
        return int(days.group(1))
    return 0


def simplyhired_jobs(page, category, now):
    stripped = re.sub(r"<style[\s\S]*?</style>", "", page, flags=re.I)
    found = []
    seen = set()
    for part in stripped.split('data-testid="searchSerpJobTitle"')[1:]:
        chunk = part[:5000]
        href = re.search(r'href="(/job/[^"?#]+)', chunk)
        title_match = re.search(r">([^<]+)</a>", chunk)
        if not href or not title_match:
            continue
        title = clean(title_match.group(1))
        company = re.search(r'data-testid="companyName"[^>]*>([\s\S]*?)</span>', chunk)
        location = re.search(r'data-testid="searchSerpJobLocation"[^>]*>([\s\S]*?)</span>', chunk)
        stamp = re.search(r'data-testid="searchSerpJobDateStamp"[^>]*>([\s\S]{0,40})', chunk)
        position = re.search(r'data-testid="jobTypeChip-0"[^>]*>([\s\S]*?)</li>', chunk)
        pay = re.search(r"\$\d[\d,]*(?:\s*[–-]\s*\$?\d[\d,]*)?(?:\s*(?:an hour|a year|/hour|/year|/yr|/hr))?", clean(chunk), re.I)
        link = href.group(1)
        card = clean(re.split(r'data-testid="viewJob|I want to receive the latest job alert', chunk, maxsplit=1)[0])
        if link in seen or not matches_category(category, title, card):
            continue
        seen.add(link)
        found.append(listing({
            "id": "sh-" + link.rstrip("/").rsplit("/", 1)[-1],
            "category": category,
            "title": title,
            "client": company.group(1) if company else "",
            "location": location.group(1) if location else "",
            "pay": pay.group(0) if pay else "",
            "posted": now - timedelta(days=relative_days(stamp.group(1) if stamp else "")),
            "positionType": position.group(1) if position else "",
            "url": "https://www.simplyhired.com" + link,
            "summary": "Public SimplyHired listing.",
        }, now))
    return [job for job in found if job]


def search_simplyhired(category, query, now):
    url = f"https://www.simplyhired.com/search?q={quote_plus(query)}&l=United%20States&t=7"
    return simplyhired_jobs(fetch(url), category, now)


def tag_text(block, tag):
    found = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", block, re.I)
    return clean(found.group(1) if found else "")


def wwr_jobs(xml, now):
    found = []
    for block in re.findall(r"<item>([\s\S]*?)</item>", xml):
        raw_title = tag_text(block, "title")
        split_at = raw_title.find(": ")
        client = raw_title[:split_at] if split_at > 0 else "Company not listed"
        title = raw_title[split_at + 2 :] if split_at > 0 else raw_title
        summary = " ".join(part for part in (tag_text(block, "description"), tag_text(block, "skills")) if part)
        category = classify(title, summary)
        if not category:
            continue
        posted = tag_text(block, "pubDate")
        try:
            when = datetime.strptime(posted, "%a, %d %b %Y %H:%M:%S %z")
        except ValueError:
            continue
        region = ", ".join(part for part in (tag_text(block, "region"), tag_text(block, "state"), tag_text(block, "country")) if part)
        found.append(listing({
            "id": "wwr-" + (tag_text(block, "guid") or tag_text(block, "link")),
            "category": category,
            "title": title,
            "client": client,
            "location": region or "Remote",
            "posted": when,
            "positionType": tag_text(block, "type"),
            "url": tag_text(block, "link"),
            "summary": summary,
            "source": "We Work Remotely",
        }, now))
    return [job for job in found if job]


def money(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    if amount <= 0:
        return ""
    return f"${amount:,.0f}"


def remote_ok_jobs(payload, now):
    found = []
    for job in json.loads(payload):
        if not isinstance(job, dict) or not job.get("position") or not job.get("id"):
            continue
        summary = f"{clean(job.get('description'))} {' '.join(job.get('tags') or [])}"
        category = classify(job["position"], summary)
        if not category or not job.get("date"):
            continue
        minimum = money(job.get("salary_min"))
        maximum = money(job.get("salary_max"))
        found.append(listing({
            "id": f"rok-{job['id']}",
            "category": category,
            "title": job["position"],
            "client": job.get("company"),
            "location": job.get("location") or "Remote",
            "pay": f"{minimum}–{maximum}" if minimum and maximum else "",
            "posted": datetime.fromisoformat(job["date"].replace("Z", "+00:00")),
            "url": job.get("url") or job.get("apply_url"),
            "summary": summary,
            "source": "Remote OK",
        }, now))
    return [job for job in found if job]


def jobicy_jobs(payload, now):
    found = []
    for job in json.loads(payload).get("jobs") or []:
        summary = clean(job.get("jobDescription") or job.get("jobExcerpt"))
        category = classify(job.get("jobTitle") or "", summary)
        if not category or not job.get("pubDate"):
            continue
        minimum = money(job.get("salaryMin"))
        maximum = money(job.get("salaryMax"))
        period = "hour" if job.get("salaryPeriod") == "hourly" else "year"
        kind = job.get("jobType")
        found.append(listing({
            "id": f"jobicy-{job.get('id')}",
            "category": category,
            "title": job.get("jobTitle"),
            "client": job.get("companyName"),
            "location": job.get("jobGeo") or "Remote",
            "pay": f"{minimum}–{maximum} / {period}" if minimum and maximum else "",
            "posted": datetime.fromisoformat(job["pubDate"].replace("Z", "+00:00")),
            "positionType": ", ".join(kind) if isinstance(kind, list) else kind,
            "url": job.get("url"),
            "summary": summary,
            "source": "Jobicy",
        }, now))
    return [job for job in found if job]


def block_field(block, tag):
    found = re.search(rf"<{tag}><!\[CDATA\[([\s\S]*?)\]\]></{tag}>", block, re.I) or re.search(rf"<{tag}>([\s\S]*?)</{tag}>", block, re.I)
    return clean(found.group(1) if found else "")


def devit_jobs(xml, now):
    found = []
    seen = set()
    for needle in ("boomi", "Boomi", "playwright", "Playwright", "React", "react", "Django", "FastAPI", "Spring Boot"):
        start_at = 0
        while True:
            at = xml.find(needle, start_at)
            if at < 0:
                break
            start_at = at + len(needle)
            start = xml.rfind("<job ", 0, at)
            end = xml.find("</job>", at)
            if start < 0 or end < 0 or start in seen:
                continue
            seen.add(start)
            block = xml[start:end]
            title = block_field(block, "title")
            summary = block_field(block, "description")
            category = classify(title, summary)
            posted_match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", block_field(block, "pubdate"))
            if not category or not posted_match:
                continue
            posted = datetime(int(posted_match.group(3)), int(posted_match.group(2)), int(posted_match.group(1)), 12, tzinfo=timezone.utc)
            location = ", ".join(part for part in (block_field(block, "location"), block_field(block, "city"), block_field(block, "region")) if part)
            found.append(listing({
                "id": "devit-" + (block_field(block, "id") or str(start)),
                "category": category,
                "title": title,
                "client": block_field(block, "company"),
                "location": location,
                "pay": block_field(block, "salary"),
                "posted": posted,
                "positionType": block_field(block, "jobtype"),
                "url": block_field(block, "url") or block_field(block, "link"),
                "summary": summary,
                "source": "DevITjobs",
            }, now))
    return [job for job in found if job]


def public_boards(now):
    global LAST_DEVIT
    jobs = []
    notes = []
    tasks = (
        ("We Work Remotely", lambda: wwr_jobs(fetch("https://weworkremotely.com/remote-jobs.rss"), now)),
        ("Remote OK", lambda: remote_ok_jobs(fetch("https://remoteok.com/api"), now)),
        ("Jobicy Python", lambda: jobicy_jobs(fetch("https://jobicy.com/api/v2/remote-jobs?count=50&tag=python"), now)),
        ("Jobicy Java", lambda: jobicy_jobs(fetch("https://jobicy.com/api/v2/remote-jobs?count=50&tag=java"), now)),
        ("Jobicy React", lambda: jobicy_jobs(fetch("https://jobicy.com/api/v2/remote-jobs?count=50&tag=react"), now)),
    )
    for name, task in tasks:
        try:
            found = task()
            jobs.extend(found)
            notes.append(f"{name}: {len(found)}")
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as error:
            notes.append(f"{name}: {error.__class__.__name__}")
    if time.monotonic() - LAST_DEVIT > 3600:
        LAST_DEVIT = time.monotonic()
        try:
            found = devit_jobs(fetch("https://devitjobs.com/job_feed.xml"), now)
            jobs.extend(found)
            notes.append(f"DevITjobs: {len(found)}")
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            notes.append(f"DevITjobs: {error.__class__.__name__}")
    return jobs, notes


def search_source(source, category, query, slot, now):
    if source == "dice":
        return search_dice(category, query, (slot % 3) + 1, now)
    if source == "linkedin":
        return search_linkedin(category, query, (slot % 3) * 25, now)
    if source == "simplyhired":
        return search_simplyhired(category, query, now)
    return []


def maybe_alert(opened):
    if time.monotonic() - STARTED < 90:
        mark_seen(opened)
    else:
        send_alert(opened)


def crawl_forever(crawler):
    slot = 0
    while True:
        if not WATCHER.is_set():
            time.sleep(1)
            continue
        source = SOURCES[slot % len(SOURCES)]
        query_slot = slot // len(SOURCES)
        query = crawler["queries"][query_slot % len(crawler["queries"])]
        slot += 1
        now = datetime.now(timezone.utc)
        notes = [f"{crawler['label']} crawler"]
        opened = []
        try:
            if source == "boards":
                if crawler["id"] != "boomi":
                    time.sleep(20)
                    continue
                found, board_notes = public_boards(now)
                opened.extend(remember(found))
                notes.extend(board_notes)
            else:
                found = search_source(source, crawler["id"], query, query_slot, now)
                opened.extend(remember(found))
                notes.append(f"{crawler['label']}: {len(found)} from {source} “{query}”")
                if crawler["id"] == "boomi":
                    extra = search_source(source, "ai-engineer", "AI Engineer", query_slot, now)
                    opened.extend(remember(extra))
                    if extra:
                        notes.append(f"AI Engineer: {len(extra)} from {source}")
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError, KeyError) as error:
            notes.append(f"{crawler['label']}: {source} {error.__class__.__name__}")
        maybe_alert(opened)
        with LOCK:
            counts = {item["label"]: sum(1 for job in JOBS.values() if job["category"] == item["id"]) for item in CRAWLERS}
        notes.append("This week: " + ", ".join(f"{label} {count}" for label, count in counts.items()))
        publish(notes)
        time.sleep(20)


def watch():
    for crawler in CRAWLERS:
        threading.Thread(target=crawl_forever, args=(crawler,), name=crawler["id"], daemon=True).start()
