"""Boomi job portal API for the website and the Flutter deck."""

import json
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from alerts import add_subscriber, mail_ready
from feed import snapshot, watch

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "website" / "kforce-boomi-integration.html"
DECISIONS = Path(__file__).resolve().parent / "decisions.json"
EASTERN = ZoneInfo("America/New_York")
ACTIONS = {"apply", "reject", "save", "share"}

app = FastAPI(title="Boomi jobs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class DecisionIn(BaseModel):
    action: str = Field(pattern="^(apply|reject|save|share)$")


class SubscribeIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_decisions():
    if not DECISIONS.exists():
        return {}
    try:
        return json.loads(DECISIONS.read_text())
    except json.JSONDecodeError:
        return {}


def save_decisions(decisions):
    DECISIONS.write_text(json.dumps(decisions, indent=2))


@app.on_event("startup")
def start_watch() -> None:
    threading.Thread(target=watch, daemon=True).start()


@app.get("/api/jobs")
def jobs():
    return snapshot()


@app.get("/api/decisions")
def decisions():
    return load_decisions()


@app.post("/api/jobs/{job_id}/decision")
def decide(job_id: str, body: DecisionIn):
    if body.action not in ACTIONS:
        raise HTTPException(status_code=400, detail="Unknown action")
    feed = snapshot()
    job = next((item for item in feed["jobs"] if item["id"] == job_id), None)
    if job is None and job_id not in load_decisions():
        raise HTTPException(status_code=404, detail="Job is not in the current week")
    decisions = load_decisions()
    decisions[job_id] = {
        "action": body.action,
        "title": (job or {}).get("title", ""),
        "client": (job or {}).get("client", ""),
        "url": (job or {}).get("url", ""),
        "at": datetime.now(EASTERN).isoformat(timespec="seconds"),
    }
    save_decisions(decisions)
    return decisions[job_id]


@app.post("/api/subscribe")
def subscribe(body: SubscribeIn):
    address = add_subscriber(body.email)
    return {
        "email": address,
        "from": "perspectivecomputing@gmail.com",
        "sending": mail_ready(),
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def website():
    if PAGE.exists():
        return FileResponse(PAGE)
    return {"ok": True, "service": "agamotto-watcher"}


@app.get("/agamotto.svg")
def logo():
    return FileResponse(PAGE.parent / "agamotto.svg", media_type="image/svg+xml")
