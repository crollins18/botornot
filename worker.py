#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Caleb Rollins
"""
A botornot worker: asks the orchestrator for one small unit of work, does it, reports back, repeats.

  BOTORNOT_ORCHESTRATOR=http://orchestrator:8080/botornot python worker.py

Run as many as you like; they are all the same and keep nothing. The orchestrator decides when
each unit goes out, so more workers do not mean more requests to YouTube per second (see
orchestrator.py). A unit is one of: list a channel's videos and posts, read one video's or post's
comments, or fetch one commenter's profile card and score them.

The meaning check runs here when a unit asks for it and the packages in
requirements-meaning.txt are installed; the model is loaded on the first such unit.
"""
import logging
import os
import socket
import time

import requests

import botornot

log = logging.getLogger("botornot.worker")

ORCHESTRATOR = os.environ.get("BOTORNOT_ORCHESTRATOR", "http://orchestrator:8080/botornot").rstrip("/")
NAME = os.environ.get("BOTORNOT_WORKER", socket.gethostname())

yt = botornot.YouTube()
model_error = None  # why the meaning model could not be loaded, once that has been tried and failed


def with_meaning(wanted):
    """Whether this unit's scoring uses the meaning model, loading it on first need."""
    global model_error
    if not wanted or model_error:
        return False
    if botornot.embedder is None:
        model_error = botornot.load_embedder()
        if model_error:
            log.error("scoring without the meaning check from now on: %s", model_error)
            return False
    return True


def do(kind, p):
    """Carry out one unit and return its result. Raises YouTubeError when YouTube does not cooperate."""
    if p.get("client_version"):
        yt.client_version = p["client_version"]
    if kind == "channel":
        owner, name, items = yt.channel(p["path"])
        return {"owner": owner, "name": name, "items": items, "client_version": yt.client_version}
    if kind == "comments":
        owner, token = yt.open(p["kind"], p["item_id"])
        return {"owner": owner, "commenters": yt.commenters(token), "client_version": yt.client_version}
    if kind == "card":
        card, why = yt.profile_card(p["commenter"], p["owner"], p["author"]["comments"][0]["id"])
        comments = botornot.all_comments(p["author"], card)
        scored = botornot.score(comments, card, meaning=with_meaning(p.get("meaning")))
        return {"account": botornot.account_row(p["commenter"], p["author"], card, why, scored), "comments": comments}
    raise ValueError(f"unknown kind of unit: {kind}")


def call(path, body, tries=1):
    """POST to the orchestrator and return its JSON reply, or None when it cannot be reached."""
    for attempt in range(1, tries + 1):
        try:
            r = requests.post(f"{ORCHESTRATOR}{path}", json=body, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("orchestrator %s not reachable (attempt %d of %d): %s", path, attempt, tries, type(e).__name__)
            if attempt < tries:
                time.sleep(5)
    return None


class Capture(logging.Handler):
    """Collects what is logged while a unit is done, to send along with its result."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append({"ts": record.created, "level": record.levelname, "message": record.getMessage()})


def run(unit):
    """Do one unit and return the report for the orchestrator: result or error, rate-limit count, log lines."""
    started, seen, capture = time.monotonic(), yt.rate_limited, Capture()
    botornot.log.addHandler(capture)  # also catches this module's logger, which sits under botornot's
    body = {"worker": NAME, "unit": unit["id"], "ok": True}
    try:
        log.debug("unit %d (%s) received", unit["id"], unit["kind"])
        try:
            body["result"] = do(unit["kind"], unit["payload"])
        except botornot.YouTubeError as e:
            body.update(ok=False, error=str(e))
        except Exception as e:  # a bug or an unexpected reply: report it, do not die
            log.exception("unit %d (%s) raised an unexpected error", unit["id"], unit["kind"])
            body.update(ok=False, error=f"{type(e).__name__}: {e}")
        body["rate_limited"] = yt.rate_limited - seen
        log.log(logging.INFO if body["ok"] else logging.WARNING, "unit %d (%s) %s in %.1f s%s", unit["id"], unit["kind"],
                "done" if body["ok"] else "failed", time.monotonic() - started,
                "" if body["ok"] else f": {body['error']}")
    finally:
        botornot.log.removeHandler(capture)
    body["logs"] = capture.lines
    return body


def main():
    botornot.setup_logging()
    # everything from DEBUG up goes to the orchestrator with each unit (the scan's log view);
    # this process's own output stays at the level that was asked for
    for handler in logging.getLogger().handlers:
        handler.setLevel(botornot.log.level)
    botornot.log.setLevel(logging.DEBUG)
    log.info("worker %s started; orchestrator at %s", NAME, ORCHESTRATOR)
    while True:
        reply = call("/api/lease", {"worker": NAME})
        if reply is None:
            time.sleep(5)
            continue
        if "unit" not in reply:
            time.sleep(reply.get("wait", 3))
            continue
        botornot.apply_settings(reply.get("settings", {}))  # the dials on the orchestrator's settings page
        body = run(reply["unit"])
        if call("/api/report", body, tries=5) is None:
            log.error("could not report unit %d; the orchestrator will hand it out again later", body["unit"])


if __name__ == "__main__":
    main()
