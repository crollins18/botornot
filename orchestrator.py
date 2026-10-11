#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Caleb Rollins
"""
The botornot service: a web page, a schedule and a queue of small units of work.

  python orchestrator.py            then open http://<this machine>:8080/botornot/

Channels (or single videos/posts) are added on the web page, each with its own schedule.
A scan is cut into units that worker processes (worker.py) fetch one at a time and report back on:

  channel   read the channel's Videos and Posts tabs          -> one `comments` unit per video/post
  comments  read one video's or post's comments               -> when all are in, one `card` unit per commenter
  card      fetch one commenter's profile card and score them -> an account in the scan's results

Each scan keeps its own log: what this process decided, and every line the workers logged while
doing its units (down to each request and wait). The scan's page shows it live.

Workers never decide when to send a request to YouTube; this process does, when it hands out units:
`card` units leave no faster than CARD_WORKERS per CARD_WAIT (the same rate as the command-line
tool), at most WORKERS other units are out at once, and when a worker reports rate-limiting or
three cards in a row fail, nothing is handed out for a while (1 minute, doubling to an hour at most).
So adding workers never raises the request rate: every worker reaches YouTube from the same address.

The web page has: the targets with their schedules, each scan with a progress bar, its results and its
live log, each account's history, and a settings page for the dials in botornot.py, for deleting scans,
and with a record of who changed or deleted what. Every page keeps itself current (templates/base.html).

Everything is kept in one SQLite file ($BOTORNOT_DB, default /data/botornot.db). There is no
login: run it on a home network only. The stored results contain other people's comments.
"""
import io
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from flask import Flask, Response, abort, has_request_context, redirect, render_template, request, url_for

import botornot

log = logging.getLogger("botornot.orchestrator")

DB = os.environ.get("BOTORNOT_DB", "/data/botornot.db")
PORT = 8080
PREFIX = "/" + os.environ.get("BOTORNOT_PREFIX", "/botornot").strip("/")  # the site lives under this path
PREFIX = "" if PREFIX == "/" else PREFIX                                   # (BOTORNOT_PREFIX=/ serves it at the top)
MAX_LOG_LINE = 2000        # characters kept of one log line; a worker's report is cut at MAX_UNIT_LOG lines
MAX_UNIT_LOG = 500
LEASE_TIMEOUT = 900        # seconds a worker may hold a unit before it is handed to another
UNIT_ATTEMPTS = 2          # times a unit is tried before it counts as failed
# seconds between a page's own refreshes: while a scan runs, otherwise, and for pages whose data rarely changes
LIVE_BUSY, LIVE_IDLE, LIVE_SETTLED = 3, 5, 15
WORKER_ONLINE = 60         # a worker that asked for work within this many seconds counts as online
FIRST_PAUSE = 60           # seconds nothing is handed out after trouble; doubles while trouble repeats
LONGEST_PAUSE = 3600
KEPT_EVENTS = 500          # entries kept in the activity record on the settings page
SCHEDULES = [(0, "manual only"), (1, "every hour"), (3, "every 3 hours"), (6, "every 6 hours"),
             (12, "every 12 hours"), (24, "every day"), (48, "every 2 days"), (168, "every week")]

# The dials on the settings page: (group, [(name in botornot.py, label, lowest, highest, hint)]).
# A whole number as the lowest value makes it a whole-number setting.
SETTINGS = [
    ("How much to read", [
        ("MAX_VIDEOS", "Newest videos per channel", 0, 30, "The Videos tab lists 30."),
        ("MAX_POSTS", "Newest posts per channel", 0, 30, "The Posts tab lists about 10."),
        ("MAX_COMMENTS", "Comments per video or post", 20, 2000, "Newest top-level comments; read 20 per request."),
        ("MAX_AUTHORS", "Commenters checked per scan", 1, 1000, "One profile-card request each. Those seen on the most videos/posts go first."),
    ]),
    ("Scoring", [
        ("W_CROSS_VIDEO_COPY", "Points: near-identical comments on different videos", 0, 10, ""),
        ("W_REPEAT_ON_VIDEO", "Points: near-identical comments on the same video or post", 0, 10, ""),
        ("W_SAME_MEANING", "Points: reworded comments with the same meaning", 0, 10, "Only for targets with the meaning check on."),
        ("W_NEW_ACCOUNT", "Points: very new account", 0, 10, ""),
        ("SIMILARITY", "Text similarity that counts as near-identical", 0.5, 1.0, "0 to 1; 1 is identical."),
        ("MEANING_SIMILARITY", "Meaning similarity that counts as reworded", 0.5, 1.0, "Unrelated comments sit near 0.75, rewordings above 0.90."),
        ("MIN_TEXT_LEN", "Shortest comment compared (characters)", 1, 500, "Shorter ones match by chance."),
        ("NEW_ACCOUNT_YEARS", "An account is very new below this age (years)", 0.0, 5.0, "0.1 is about 5 weeks."),
    ]),
    ("Pace (requests to YouTube)", [
        ("CARD_WORKERS", "Profile cards requested at the same time", 1, 8, ""),
        ("CARD_WAIT_MIN", "Shortest wait between one card worker's requests (seconds)", 1.0, 120.0, ""),
        ("CARD_WAIT_MAX", "Longest wait between one card worker's requests (seconds)", 1.0, 120.0, ""),
        ("WORKERS", "Videos/posts whose comments are read at the same time", 1, 8, ""),
        ("PAGE_WAIT_MIN", "Shortest wait between comment pages (seconds)", 0.5, 30.0, ""),
        ("PAGE_WAIT_MAX", "Longest wait between comment pages (seconds)", 0.5, 30.0, ""),
    ]),
]


def setting_value(name):
    """The value botornot.py has for a setting now (ranges are split into NAME_MIN and NAME_MAX)."""
    if name.endswith(("_MIN", "_MAX")):
        return getattr(botornot, name[:-4])[name.endswith("_MAX")]
    return getattr(botornot, name)


DEFAULTS = {name: setting_value(name) for _, dials in SETTINGS for name, *_ in dials}  # as botornot.py ships

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, name TEXT,
    every_hours REAL NOT NULL DEFAULT 0, meaning INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1, next_run REAL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
    started REAL NOT NULL, finished REAL, owner TEXT, client_version TEXT, note TEXT, meaning INTEGER,
    commenters INTEGER, checked INTEGER NOT NULL DEFAULT 0, flagged INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, worker TEXT,
    leased_at REAL, finished_at REAL, error TEXT, result TEXT);
CREATE INDEX IF NOT EXISTS units_by_status ON units (status, id);
CREATE INDEX IF NOT EXISTS units_by_scan ON units (scan_id);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL, commenter_id TEXT NOT NULL, score INTEGER NOT NULL,
    name TEXT, handle TEXT, data TEXT NOT NULL, comments TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS accounts_by_scan ON accounts (scan_id);
CREATE INDEX IF NOT EXISTS accounts_by_commenter ON accounts (commenter_id);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL, ts REAL NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL,
    message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS logs_by_scan ON logs (scan_id, id);
CREATE TABLE IF NOT EXISTS workers (name TEXT PRIMARY KEY, last_seen REAL NOT NULL, units_done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, who TEXT NOT NULL, message TEXT NOT NULL);
"""

app = Flask(__name__)
db_lock = threading.RLock()  # one database user at a time: web requests, worker calls and the scheduler


@contextmanager
def db():
    with db_lock:
        con = sqlite3.connect(DB, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA synchronous = NORMAL")  # safe with the write-ahead log, and far fewer disk flushes
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()


def get_state(con, key, default=0.0):
    row = con.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return type(default)(row["value"]) if row else default


def set_state(con, key, value):
    con.execute("INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, str(value)))


def note(con, level, message, *args):
    """Log something a person did on the web page, and keep it in the activity record (with their address)."""
    log.log(level, message, *args)
    who = (request.remote_addr or "unknown") if has_request_context() else "the service itself"
    con.execute("INSERT INTO events (ts, who, message) VALUES (?, ?, ?)", (time.time(), who, message % args))
    con.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (KEPT_EVENTS,))


def add_unit(con, scan_id, kind, /, **payload):
    con.execute("INSERT INTO units (scan_id, kind, payload) VALUES (?, ?, ?)", (scan_id, kind, json.dumps(payload)))


# ---- scans -------------------------------------------------------------------------------------

def keep_log(con, scan_id, ts, level, source, message):
    con.execute("INSERT INTO logs (scan_id, ts, level, source, message) VALUES (?, ?, ?, ?, ?)",
                (scan_id, ts, level, source, message[:MAX_LOG_LINE]))


def scan_log(con, scan_id, level, message, *args):
    """Log a line about a scan, and keep it for the log view on the scan's page."""
    log.log(level, "scan %d: " + message, scan_id, *args)
    keep_log(con, scan_id, time.time(), logging.getLevelName(level), "orchestrator", message % args)


def start_scan(con, channel, why):
    """Create a scan of a channel row and queue its first unit. Returns the scan ID."""
    now = time.time()
    is_channel = channel["kind"] == "channel"
    scan_id = con.execute("INSERT INTO scans (channel_id, status, stage, started, meaning) VALUES (?, 'running', ?, ?, ?)",
                          (channel["id"], "listing" if is_channel else "comments", now, channel["meaning"])).lastrowid
    if is_channel:
        add_unit(con, scan_id, "channel", path=channel["target"])
    else:
        add_unit(con, scan_id, "comments", kind=channel["kind"], item_id=channel["target"],
                 label=f"this {channel['kind']}")
    if channel["every_hours"]:
        con.execute("UPDATE channels SET next_run = ? WHERE id = ?", (now + channel["every_hours"] * 3600, channel["id"]))
    scan_log(con, scan_id, logging.INFO, "scan of %s started (%s)%s", channel["target"], why,
             ", with the meaning check" if channel["meaning"] else "")
    return scan_id


def end_scan(con, scan_id, status, note=None):
    stats = con.execute("SELECT COUNT(*) AS checked, COALESCE(SUM(score > 0), 0) AS flagged FROM accounts "
                        "WHERE scan_id = ?", (scan_id,)).fetchone()
    con.execute("UPDATE scans SET status = ?, finished = ?, note = COALESCE(?, note), checked = ?, flagged = ? WHERE id = ?",
                (status, time.time(), note, stats["checked"], stats["flagged"], scan_id))
    con.execute("UPDATE units SET status = 'cancelled' WHERE scan_id = ? AND status IN ('queued', 'leased')", (scan_id,))
    con.execute("UPDATE units SET result = NULL WHERE scan_id = ?", (scan_id,))  # the comment lists are no longer needed
    scan = con.execute("SELECT started FROM scans WHERE id = ?", (scan_id,)).fetchone()
    scan_log(con, scan_id, {"done": logging.INFO, "failed": logging.ERROR}.get(status, logging.WARNING),
             "%s after %.0f s: %d accounts checked, %d with score > 0%s", status,
             time.time() - scan["started"], stats["checked"], stats["flagged"], f" ({note})" if note else "")


def plan_cards(con, scan):
    """All comments of the scan are in: merge the commenters and queue one `card` unit for each to check."""
    authors, owner, version, read, skipped = {}, scan["owner"], scan["client_version"], 0, 0
    for unit in con.execute("SELECT * FROM units WHERE scan_id = ? AND kind = 'comments' ORDER BY id", (scan["id"],)):
        if unit["status"] != "done":
            skipped += 1
            continue
        item, result = json.loads(unit["payload"]), json.loads(unit["result"])
        owner, version, read = owner or result["owner"], result["client_version"], read + 1
        botornot.merge_commenters(authors, result["commenters"], item["kind"], item["item_id"], item["label"])
    todo = botornot.pick_commenters(authors, owner)
    note = f"{skipped} of {read + skipped} videos/posts could not be read" if skipped else None
    if not todo:
        return end_scan(con, scan["id"], "failed", "no comments could be read" if not authors
                        else "the only top-level comments are from the channel owner")
    for commenter, author in todo:
        add_unit(con, scan["id"], "card", commenter=commenter, owner=owner, author=author,
                 client_version=version, meaning=bool(scan["meaning"]))
    con.execute("UPDATE scans SET stage = 'cards', owner = ?, commenters = ?, note = ? WHERE id = ?",
                (owner, len(authors) - (owner in authors), note, scan["id"]))
    scan_log(con, scan["id"], logging.WARNING if skipped else logging.INFO,
             "%d videos/posts read, %d commenters, %d profile cards queued%s", read,
             len(authors), len(todo), f" ({note})" if note else "")


def advance(con, scan_id):
    """Move a scan on once none of its units are waiting or out with a worker."""
    scan = con.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if scan["status"] != "running" or con.execute(
            "SELECT 1 FROM units WHERE scan_id = ? AND status IN ('queued', 'leased')", (scan_id,)).fetchone():
        return
    if scan["stage"] == "comments":
        plan_cards(con, scan)
    elif scan["stage"] == "cards":
        end_scan(con, scan_id, "done")


def trouble(con, reason):
    """YouTube is pushing back: hand out nothing for a while, longer each time it happens again."""
    level = get_state(con, "trouble_level", 0) + 1
    seconds = min(FIRST_PAUSE * 2 ** (level - 1), LONGEST_PAUSE)
    set_state(con, "trouble_level", level)
    set_state(con, "paused_until", time.time() + seconds)
    set_state(con, "paused_why", reason)
    set_state(con, "failed_cards", 0)
    log.warning("pausing all work for %d s: %s", seconds, reason)
    for scan in con.execute("SELECT id FROM scans WHERE status = 'running'").fetchall():
        keep_log(con, scan["id"], time.time(), "WARNING", "orchestrator", f"all work paused for {seconds} s: {reason}")


# ---- the work queue (called by workers) ----------------------------------------------------------

def lease(worker):
    """Hand the next unit to a worker as {"unit": ...}, or {"wait": seconds} when there is none for it now."""
    now = time.time()
    with db() as con:
        con.execute("INSERT INTO workers (name, last_seen) VALUES (?, ?) "
                    "ON CONFLICT (name) DO UPDATE SET last_seen = excluded.last_seen", (worker, now))
        for lost in con.execute("SELECT id, scan_id, kind, worker FROM units WHERE status = 'leased' AND leased_at < ?",
                                (now - LEASE_TIMEOUT,)).fetchall():
            scan_log(con, lost["scan_id"], logging.WARNING, "unit %d (%s) was not reported back by %s within %d s; "
                     "queued again", lost["id"], lost["kind"], lost["worker"], LEASE_TIMEOUT)
            con.execute("UPDATE units SET status = 'queued', worker = NULL WHERE id = ?", (lost["id"],))
        paused = get_state(con, "paused_until") - now
        if paused > 0:
            return {"wait": min(paused, 30)}
        unit, wait = None, 3
        out = con.execute("SELECT COUNT(*) FROM units WHERE status = 'leased' AND kind != 'card'").fetchone()[0]
        if out < botornot.WORKERS:
            unit = con.execute("SELECT * FROM units WHERE status = 'queued' AND kind != 'card' ORDER BY id LIMIT 1").fetchone()
        if not unit:
            unit = con.execute("SELECT * FROM units WHERE status = 'queued' AND kind = 'card' ORDER BY id LIMIT 1").fetchone()
            next_card = get_state(con, "next_card_at")
            if unit and now < next_card:
                unit, wait = None, next_card - now
            elif unit:  # the same pace as CARD_WORKERS threads that each wait CARD_WAIT
                set_state(con, "next_card_at", now + botornot.wait_time(*botornot.CARD_WAIT) / botornot.CARD_WORKERS)
        if not unit:
            return {"wait": round(wait, 2)}
        con.execute("UPDATE units SET status = 'leased', worker = ?, leased_at = ?, attempts = attempts + 1 WHERE id = ?",
                    (worker, now, unit["id"]))
        log.debug("unit %d (%s, scan %d, attempt %d) handed to %s", unit["id"], unit["kind"], unit["scan_id"],
                  unit["attempts"] + 1, worker)
        return {"unit": {"id": unit["id"], "kind": unit["kind"], "payload": json.loads(unit["payload"])},
                "settings": {name: setting_value(name) for name in DEFAULTS}}


def report(worker, unit_id, ok, result, error, rate_limited, lines=()):
    """Take a worker's result for a unit (and the log lines it wrote meanwhile), and queue whatever follows from it."""
    with db() as con:
        unit = con.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
        if not unit or unit["status"] != "leased" or unit["worker"] != worker:
            log.warning("ignored a report from %s for unit %s, which it does not hold (%s)", worker, unit_id,
                        unit["status"] if unit else "no such unit")
            return
        scan_id, kind, payload = unit["scan_id"], unit["kind"], json.loads(unit["payload"])
        con.execute("UPDATE workers SET units_done = units_done + 1 WHERE name = ?", (worker,))
        for line in list(lines)[:MAX_UNIT_LOG]:
            keep_log(con, scan_id, float(line["ts"]), str(line["level"])[:10], worker, str(line["message"]))
        if rate_limited:
            trouble(con, f"{worker} was rate-limited by YouTube (HTTP 429) {rate_limited} time(s) on a {kind} unit")
        if not ok:
            again = unit["attempts"] < UNIT_ATTEMPTS
            con.execute("UPDATE units SET status = ?, error = ?, finished_at = ?, worker = NULL WHERE id = ?",
                        ("queued" if again else "failed", error, time.time(), unit_id))
            scan_log(con, scan_id, logging.WARNING if again else logging.ERROR, "%s unit %d failed on %s (attempt %d of %d): %s",
                     kind, unit_id, worker, unit["attempts"], UNIT_ATTEMPTS, error)
            if kind == "channel" and not again:
                end_scan(con, scan_id, "failed", error)
            return advance(con, scan_id)

        con.execute("UPDATE units SET status = 'done', finished_at = ?, result = ? WHERE id = ?",
                    (time.time(), json.dumps(result) if kind == "comments" else None, unit_id))
        if kind == "channel":
            con.execute("UPDATE scans SET stage = 'comments', owner = ?, client_version = ? WHERE id = ?",
                        (result["owner"], result["client_version"], scan_id))
            con.execute("UPDATE channels SET name = ? WHERE id = (SELECT channel_id FROM scans WHERE id = ?)",
                        (result["name"], scan_id))
            for item_kind, item_id, label in result["items"]:
                add_unit(con, scan_id, "comments", kind=item_kind, item_id=item_id, label=label,
                         client_version=result["client_version"])
            scan_log(con, scan_id, logging.INFO, "%s lists %d videos/posts to read", result["name"], len(result["items"]))
        elif kind == "comments":
            scan_log(con, scan_id, logging.INFO, "%s %s has %d comments from %d commenters (%s)", payload["kind"],
                     payload["item_id"], sum(len(a["comments"]) for a in result["commenters"].values()),
                     len(result["commenters"]), worker)
        elif kind == "card":
            account = result["account"]
            con.execute("INSERT INTO accounts (scan_id, commenter_id, score, name, handle, data, comments) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (scan_id, payload["commenter"], account["score"], account["name"],
                                                         account["handle"], json.dumps(account), json.dumps(result["comments"])))
            con.execute("UPDATE scans SET checked = checked + 1, flagged = flagged + ? WHERE id = ?",
                        (account["score"] > 0, scan_id))
            if account["card"] == "ok":
                set_state(con, "failed_cards", 0)
                if not rate_limited:
                    set_state(con, "trouble_level", 0)
                scan_log(con, scan_id, logging.INFO if account["score"] else logging.DEBUG, "%s scored %d%s (%s)",
                         account["name"], account["score"], f": {account['reasons']}" if account["reasons"] else "", worker)
            else:
                failed = get_state(con, "failed_cards", 0) + 1
                set_state(con, "failed_cards", failed)
                scan_log(con, scan_id, logging.WARNING, "no profile card for %s: %s (%s)", account["name"], account["card"], worker)
                if failed >= 3:
                    trouble(con, "3 profile cards in a row failed; YouTube is probably blocking these requests")
        advance(con, scan_id)


def scheduler():
    """Start the scans that are due. Runs for the life of the process."""
    while True:
        try:
            with db() as con:
                for channel in con.execute(
                        "SELECT * FROM channels c WHERE enabled AND every_hours > 0 AND next_run <= ? AND NOT EXISTS "
                        "(SELECT 1 FROM scans WHERE channel_id = c.id AND status = 'running')", (time.time(),)).fetchall():
                    start_scan(con, channel, "scheduled")
                con.execute("DELETE FROM workers WHERE last_seen < ?", (time.time() - 86400,))
        except Exception:
            log.exception("the scheduler hit an error; it carries on")
        time.sleep(20)


# ---- web pages -------------------------------------------------------------------------------------

@app.template_filter("ago")
def ago(ts, future=False):
    """A timestamp as "5 min ago" (or "in 5 min")."""
    if not ts:
        return "never" if not future else "not scheduled"
    seconds = abs(time.time() - ts)
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "min")):
        if seconds >= size:
            n = int(seconds // size)
            text = f"{n} {unit}{'s' if n > 1 and unit != 'min' else ''}"
            break
    else:
        text = "under a minute"
    if future:
        return "due now" if ts <= time.time() else f"in {text}"
    return f"{text} ago"


@app.template_filter("until")
def until(ts):
    return ago(ts, future=True)


@app.template_filter("stamp")
def stamp(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else ""


@app.template_filter("took")
def took(scan):
    seconds = int((scan["finished"] or time.time()) - scan["started"])
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


@app.context_processor
def page_context():
    return {"schedules": SCHEDULES, "schedule_names": dict(SCHEDULES)}


SCAN_COLUMNS = ("s.*, c.target, c.name AS channel_name, "
                "(SELECT COUNT(*) FROM units WHERE scan_id = s.id AND kind = 'card') AS cards, "
                "(SELECT COUNT(*) FROM units WHERE scan_id = s.id AND kind = 'comments') AS items, "
                "(SELECT COUNT(*) FROM units WHERE scan_id = s.id AND kind = 'comments' AND status = 'done') AS items_done, "
                "(SELECT COUNT(*) FROM logs WHERE scan_id = s.id AND level IN ('ERROR', 'CRITICAL')) AS errors, "
                "(SELECT COUNT(*) FROM logs WHERE scan_id = s.id AND level = 'WARNING') AS warnings")


@app.template_filter("progress")
def progress(scan):
    """How far a scan has got, 0 to 100: listing is the first 5, reading comments the next 20, profile cards the rest."""
    if scan["status"] == "done":
        return 100
    if scan["stage"] == "listing":
        return 2
    if scan["stage"] == "comments":
        return round(5 + 20 * scan["items_done"] / max(scan["items"], 1))
    return round(25 + 75 * scan["checked"] / max(scan["cards"], 1))


@app.template_filter("doing")
def doing(scan):
    """What a scan is doing now, or what it got through, in a few words."""
    if scan["status"] != "running":
        return f"{scan['items_done']} videos/posts, {scan['checked']} of {scan['cards']} cards"
    if scan["stage"] == "listing":
        return "listing videos and posts"
    if scan["stage"] == "comments":
        return f"reading comments: {scan['items_done']} of {scan['items']} videos/posts"
    return f"profile cards: {scan['checked']} of {scan['cards']}"


@app.get("/")
def index():
    now = time.time()
    with db() as con:
        channels = con.execute(
            "SELECT c.*, (SELECT id FROM scans WHERE channel_id = c.id ORDER BY id DESC LIMIT 1) AS last_scan, "
            "(SELECT COUNT(*) FROM scans WHERE channel_id = c.id) AS scans FROM channels c ORDER BY c.created").fetchall()
        last = {s["id"]: s for s in con.execute(
            "SELECT * FROM scans WHERE id IN (SELECT MAX(id) FROM scans GROUP BY channel_id)")}
        scans = con.execute(f"SELECT {SCAN_COLUMNS} FROM scans s JOIN channels c ON c.id = s.channel_id "
                            "ORDER BY s.id DESC LIMIT 25").fetchall()
        workers = con.execute("SELECT * FROM workers ORDER BY name").fetchall()
        queue = dict(con.execute("SELECT status, COUNT(*) FROM units WHERE status IN ('queued', 'leased') GROUP BY status").fetchall())
        paused = get_state(con, "paused_until") - now
        paused_why = get_state(con, "paused_why", "")
    return render_template("index.html", channels=channels, last=last, scans=scans, workers=workers, queue=queue,
                           online=[w for w in workers if now - w["last_seen"] < WORKER_ONLINE],
                           paused=paused, paused_why=paused_why, error=request.args.get("error"),
                           refresh=LIVE_BUSY if queue else LIVE_IDLE)


@app.post("/channels")
def add_channel():
    target = botornot.parse_target(request.form.get("target", "").strip())
    if not target:
        return redirect(url_for("index", error="That is not a channel, video or post link. Try @handle or a full YouTube link."))
    hours = float(request.form.get("every_hours") or 0)
    with db() as con:
        if con.execute("SELECT 1 FROM channels WHERE target = ?", (target[1],)).fetchone():
            return redirect(url_for("index", error=f"{target[1]} is already in the list."))
        con.execute("INSERT INTO channels (target, kind, every_hours, meaning, next_run, created) VALUES (?, ?, ?, ?, ?, ?)",
                    (target[1], target[0], hours, "meaning" in request.form, time.time() if hours else None, time.time()))
        note(con, logging.INFO, "added %s %s, schedule: %s, meaning check %s", target[0], target[1],
             dict(SCHEDULES).get(hours, f"every {hours} hours"), "on" if "meaning" in request.form else "off")
    return redirect(url_for("index"))


@app.post("/channels/<int:channel_id>/edit")
def edit_channel(channel_id):
    hours = float(request.form.get("every_hours") or 0)
    with db() as con:
        old = con.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone() or abort(404)
        next_run = None if not hours else old["next_run"] if old["every_hours"] == hours and old["next_run"] \
            else time.time() + hours * 3600
        con.execute("UPDATE channels SET every_hours = ?, meaning = ?, enabled = ?, next_run = ? WHERE id = ?",
                    (hours, "meaning" in request.form, "enabled" in request.form, next_run, channel_id))
        note(con, logging.INFO, "changed %s: schedule %s, meaning check %s, %s", old["target"], dict(SCHEDULES).get(hours, hours),
             "on" if "meaning" in request.form else "off", "enabled" if "enabled" in request.form else "disabled")
    return redirect(request.referrer or url_for("index"))


@app.post("/channels/<int:channel_id>/scan")
def scan_now(channel_id):
    with db() as con:
        channel = con.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone() or abort(404)
        if con.execute("SELECT 1 FROM scans WHERE channel_id = ? AND status = 'running'", (channel_id,)).fetchone():
            return redirect(url_for("index", error=f"{channel['target']} is already being scanned."))
        scan_id = start_scan(con, channel, "started by hand")
        note(con, logging.INFO, "started scan %d of %s", scan_id, channel["target"])
    return redirect(url_for("scan_page", scan_id=scan_id))


@app.post("/channels/<int:channel_id>/delete")
def delete_channel(channel_id):
    with db() as con:
        channel = con.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone() or abort(404)
        gone = delete_scans(con, "WHERE channel_id = ?", (channel_id,))
        con.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        note(con, logging.WARNING, "deleted the target %s and its %d scans", channel["target"], gone)
    return redirect(url_for("index"))


@app.get("/channels/<int:channel_id>")
def channel_page(channel_id):
    with db() as con:
        channel = con.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone() or abort(404)
        scans = con.execute(f"SELECT {SCAN_COLUMNS} FROM scans s JOIN channels c ON c.id = s.channel_id "
                            "WHERE s.channel_id = ? ORDER BY s.id DESC LIMIT 200", (channel_id,)).fetchall()
        repeat = con.execute(
            "SELECT a.commenter_id, MAX(a.name) AS name, MAX(a.handle) AS handle, COUNT(*) AS seen, "
            "SUM(a.score > 0) AS flagged, MAX(a.score) AS top, MAX(CASE WHEN a.score > 0 THEN s.started END) AS last_flagged "
            "FROM accounts a JOIN scans s ON s.id = a.scan_id WHERE s.channel_id = ? GROUP BY a.commenter_id "
            "HAVING flagged > 0 ORDER BY flagged DESC, top DESC LIMIT 200", (channel_id,)).fetchall()
    return render_template("channel.html", channel=channel, scans=scans, repeat=repeat,
                           refresh=LIVE_BUSY if any(s["status"] == "running" for s in scans) else LIVE_IDLE)


@app.get("/scans/<int:scan_id>")
def scan_page(scan_id):
    with db() as con:
        scan = con.execute(f"SELECT {SCAN_COLUMNS} FROM scans s JOIN channels c ON c.id = s.channel_id WHERE s.id = ?",
                           (scan_id,)).fetchone() or abort(404)
        rows = con.execute("SELECT * FROM accounts WHERE scan_id = ?", (scan_id,)).fetchall()
        last_errors = con.execute("SELECT source, message FROM logs WHERE scan_id = ? AND level IN ('ERROR', 'CRITICAL') "
                                  "ORDER BY id DESC LIMIT 3", (scan_id,)).fetchall()
    return render_template("scan.html", scan=scan, accounts=sorted_accounts(rows), link=botornot.comment_link,
                           last_errors=last_errors, refresh=LIVE_BUSY if scan["status"] == "running" else LIVE_SETTLED)


def sorted_accounts(rows):
    """[(account, comments)] from `accounts` rows, highest score first."""
    accounts = [(json.loads(r["data"]), json.loads(r["comments"])) for r in rows]
    return sorted(accounts, key=lambda a: (-a[0]["score"], -a[0]["max_similarity"]))


@app.get("/scans/<int:scan_id>/log")
def scan_log_lines(scan_id):
    """The scan's log lines after line `after`, with where the scan stands: what the scan page polls."""
    with db() as con:
        scan = con.execute(f"SELECT {SCAN_COLUMNS} FROM scans s JOIN channels c ON c.id = s.channel_id WHERE s.id = ?",
                           (scan_id,)).fetchone() or abort(404)
        lines = con.execute("SELECT id, ts, level, source, message FROM logs WHERE scan_id = ? AND id > ? ORDER BY id LIMIT 2000",
                            (scan_id, request.args.get("after", 0, type=int))).fetchall()
    return {"lines": [dict(line) for line in lines], "status": scan["status"], "progress": progress(scan),
            "doing": doing(scan), "checked": scan["checked"], "flagged": scan["flagged"],
            "errors": scan["errors"], "warnings": scan["warnings"]}


@app.get("/scans/<int:scan_id>/log.txt")
def scan_log_text(scan_id):
    with db() as con:
        con.execute("SELECT 1 FROM scans WHERE id = ?", (scan_id,)).fetchone() or abort(404)
        lines = con.execute("SELECT ts, level, source, message FROM logs WHERE scan_id = ? ORDER BY id", (scan_id,)).fetchall()
    text = "".join(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(line['ts']))}Z {line['level']:<7} [{line['source']}] "
                   f"{line['message']}\n" for line in lines)
    return Response(text, mimetype="text/plain")


@app.get("/scans/<int:scan_id>.csv")
def scan_csv(scan_id):
    with db() as con:
        scan = con.execute("SELECT s.id, c.target FROM scans s JOIN channels c ON c.id = s.channel_id WHERE s.id = ?",
                           (scan_id,)).fetchone() or abort(404)
        rows = con.execute("SELECT data, comments FROM accounts WHERE scan_id = ?", (scan_id,)).fetchall()
    out = io.StringIO()
    botornot.write_csv(out, [(json.loads(r["data"]), json.loads(r["comments"])) for r in rows])
    name = "".join(ch if ch.isalnum() or ch in "@-_" else "_" for ch in scan["target"])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}-scan{scan_id}.csv"'})


@app.post("/scans/<int:scan_id>/cancel")
def cancel_scan(scan_id):
    with db() as con:
        scan = con.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone() or abort(404)
        if scan["status"] == "running":
            end_scan(con, scan_id, "cancelled", "cancelled by hand")
            note(con, logging.WARNING, "cancelled scan %d", scan_id)
    return redirect(url_for("scan_page", scan_id=scan_id))


@app.get("/accounts/<commenter_id>")
def account_page(commenter_id):
    with db() as con:
        rows = con.execute("SELECT a.*, s.started, s.channel_id, c.target FROM accounts a JOIN scans s ON s.id = a.scan_id "
                           "JOIN channels c ON c.id = s.channel_id WHERE a.commenter_id = ? ORDER BY a.scan_id DESC LIMIT 100",
                           (commenter_id,)).fetchall()
    if not rows:
        abort(404)
    seen = [(r, json.loads(r["data"]), json.loads(r["comments"])) for r in rows]
    return render_template("account.html", commenter_id=commenter_id, seen=seen, link=botornot.comment_link, refresh=LIVE_SETTLED)


@app.post("/resume")
def resume():
    with db() as con:
        set_state(con, "paused_until", 0)
        set_state(con, "trouble_level", 0)
        note(con, logging.WARNING, "lifted the pause by hand")
    return redirect(url_for("index"))


@app.get("/settings")
def settings_page():
    with db() as con:
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("scans", "accounts")}
        events = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("settings.html", groups=SETTINGS, defaults=DEFAULTS, value=setting_value, counts=counts, events=events, refresh=LIVE_IDLE,
                           error=request.args.get("error"), saved=request.args.get("saved"))


@app.post("/settings")
def save_settings():
    values = {}
    for _, dials in SETTINGS:
        for name, label, lowest, highest, _ in dials:
            try:
                value = type(lowest)(request.form[name])
            except (KeyError, ValueError):
                return redirect(url_for("settings_page", error=f"{label}: enter a {'whole ' if isinstance(lowest, int) else ''}number."))
            if not lowest <= value <= highest:
                return redirect(url_for("settings_page", error=f"{label}: must be between {lowest} and {highest}."))
            values[name] = value
    for base in ("CARD_WAIT", "PAGE_WAIT"):
        if values[base + "_MIN"] > values[base + "_MAX"]:
            return redirect(url_for("settings_page", error=f"{base}: the shortest wait is longer than the longest."))
    changed = {name: value for name, value in values.items() if value != setting_value(name)}
    with db() as con:
        con.execute("DELETE FROM settings")
        con.executemany("INSERT INTO settings (name, value) VALUES (?, ?)",
                        [(name, value) for name, value in values.items() if value != DEFAULTS[name]])
        for name, value in changed.items():
            note(con, logging.WARNING, "changed the setting %s from %s to %s (default %s)", name, setting_value(name),
                 value, DEFAULTS[name])
    botornot.apply_settings(values)
    return redirect(url_for("settings_page", saved=f"Saved; {len(changed)} changed." if changed else "Nothing changed."))


@app.post("/settings/reset")
def reset_settings():
    with db() as con:
        con.execute("DELETE FROM settings")
        note(con, logging.WARNING, "put all settings back to their defaults")
    botornot.apply_settings(DEFAULTS)
    return redirect(url_for("settings_page", saved="All settings are back at their defaults."))


def delete_scans(con, where="", args=()):
    """Delete scans (all of them, or those matching `where`) with their units and accounts. Returns how many."""
    scans = f"(SELECT id FROM scans {where})"
    con.execute(f"DELETE FROM accounts WHERE scan_id IN {scans}", args)
    con.execute(f"DELETE FROM units WHERE scan_id IN {scans}", args)
    con.execute(f"DELETE FROM logs WHERE scan_id IN {scans}", args)
    return con.execute(f"DELETE FROM scans {where}", args).rowcount


@app.post("/scans/<int:scan_id>/delete")
def delete_scan(scan_id):
    with db() as con:
        scan = con.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone() or abort(404)
        delete_scans(con, "WHERE id = ?", (scan_id,))
        note(con, logging.WARNING, "deleted scan %d", scan_id)
    return redirect(url_for("channel_page", channel_id=scan["channel_id"]))


@app.post("/clean")
def clean():
    """Start fresh: delete every scan and its results. Targets, schedules and settings stay."""
    with db() as con:
        gone = delete_scans(con)
        con.execute("DELETE FROM state")  # also lifts a pause
        note(con, logging.WARNING, "deleted all %d scans and their results (start fresh)", gone)
    with db_lock:  # hand the freed space back; VACUUM cannot run inside a transaction
        con = sqlite3.connect(DB, isolation_level=None)
        con.execute("VACUUM")
        con.close()
    return redirect(url_for("settings_page", saved=f"Deleted {gone} scans and their results."))


@app.before_request
def own_pages_only():
    """Refuse a POST that another website made the browser send: this site has no login, so without
    this any page you visit could press its buttons. Workers and scripts send no Origin and pass."""
    origin = request.headers.get("Origin")
    if request.method == "POST" and (request.headers.get("Sec-Fetch-Site") == "cross-site"
                                     or (origin is not None and urlsplit(origin).netloc != request.host)):
        log.warning("refused a POST to %s sent from another site (%s)", request.path, origin)
        abort(403)


@app.post("/api/lease")
def api_lease():
    return lease(str(request.get_json(force=True)["worker"])[:80])


@app.post("/api/report")
def api_report():
    body = request.get_json(force=True)
    report(str(body["worker"])[:80], body["unit"], bool(body["ok"]), body.get("result"), body.get("error"),
           int(body.get("rate_limited") or 0), body.get("logs") or ())
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"ok": True}


class UnderPrefix:
    """Serve the app under PREFIX only: /botornot/... reaches it, / is sent there, anything else is not found."""

    def __init__(self, wsgi_app, prefix):
        self.app, self.prefix = wsgi_app, prefix

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if not self.prefix:
            return self.app(environ, start_response)
        if path == self.prefix or path.startswith(self.prefix + "/"):
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self.prefix  # url_for() then builds links under it
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            return self.app(environ, start_response)
        if path in ("", "/"):
            start_response("302 Found", [("Location", self.prefix + "/"), ("Content-Length", "0")])
            return [b""]
        body = f"Not found. The site is at {self.prefix}/\n".encode()
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))])
        return [body]


def migrate(con):
    """Bring a database made by an older version up to date."""
    for table in ("channels", "scans"):
        made = con.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()["sql"]
        if "AUTOINCREMENT" not in made:  # IDs used to be handed out again after a delete; now they never are
            con.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            con.executescript(SCHEMA)
            con.execute(f"INSERT INTO {table} SELECT * FROM {table}_old")
            con.execute(f"DROP TABLE {table}_old")
            log.info("database updated: %s IDs are no longer reused", table)


def init_db():
    """Create the database if needed and load the saved settings. Returns how many units went back in the queue."""
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    with db() as con:
        con.executescript(SCHEMA)
        migrate(con)
        con.execute("PRAGMA journal_mode = WAL")
        saved = {row["name"]: type(DEFAULTS[row["name"]])(row["value"])
                 for row in con.execute("SELECT * FROM settings") if row["name"] in DEFAULTS}
        # units that were out when this process last stopped go back in the queue
        back = con.execute("UPDATE units SET status = 'queued', worker = NULL WHERE status = 'leased'").rowcount
    botornot.apply_settings({**DEFAULTS, **saved})
    for name, value in saved.items():
        log.info("setting %s is %s (default %s)", name, value, DEFAULTS[name])
    return back


def main():
    botornot.setup_logging()
    back = init_db()
    log.info("database %s ready%s", DB, f"; {back} units from before the restart queued again" if back else "")
    threading.Thread(target=scheduler, name="scheduler", daemon=True).start()
    log.info("web page and worker API on port %d under %s/", PORT, PREFIX)
    from waitress import serve
    serve(UnderPrefix(app, PREFIX), host="0.0.0.0", port=PORT, threads=8)


if __name__ == "__main__":
    main()
