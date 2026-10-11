"""The service end to end: web page -> queue -> worker -> results, with a made-up YouTube."""
import re

import pytest

import botornot
import orchestrator
import worker


class FakeYouTube:
    """Answers like botornot.YouTube, from a tiny invented channel."""
    client_version, rate_limited = "2.fake", 0
    fail_cards_of = ()

    def channel(self, path):
        return "UCowner", "@fake", [("video", "vid00000001", "First video"), ("post", "UgkxPost", "post UgkxPost")]

    def open(self, kind, item_id):
        self.item = item_id
        return "UCowner", "token"

    def commenters(self, token):
        spam = "I made a fortune last month thanks to this one trading adviser, what a genius!"
        people = {"UCowner": {"name": "The Channel", "comments": [{"id": f"own-{self.item}", "text": "Thanks all!"}]},
                  "UCspam": {"name": "Spammer", "comments": [{"id": f"spam-{self.item}", "text": spam}]}}
        if self.item == "vid00000001":
            people["UChuman"] = {"name": "=Human", "comments": [{"id": "h1", "text": "I disagree with the second point made here."}]}
        return people

    def profile_card(self, commenter, owner, comment_id):
        assert owner == "UCowner"
        if commenter in self.fail_cards_of:
            return None, "HTTP 403 (blocked)"
        return {"name": commenter[2:].title(), "handle": "@" + commenter[2:], "joined": "Joined 5 years ago",
                "subscribers": 3, "comment_badge": "12 comments", "comments": []}, None


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A fresh orchestrator on a temporary database, and a worker wired to a fake YouTube."""
    monkeypatch.setattr(orchestrator, "DB", str(tmp_path / "test.db"))
    orchestrator.init_db()
    fake = FakeYouTube()
    monkeypatch.setattr(worker, "yt", fake)
    monkeypatch.setattr(worker, "NAME", "w1")
    level = botornot.log.level
    botornot.log.setLevel("DEBUG")  # as worker.main() sets it, so units report every line
    monkeypatch.setattr(botornot, "wait_time", lambda shortest, longest: 0.0)  # no pacing between card units in tests
    client = orchestrator.app.test_client()
    client.fake = fake
    yield client
    botornot.log.setLevel(level)


def work(client, name="w1", limit=50):
    """Play a worker until the queue is empty. Returns the kinds of unit it did."""
    done = []
    for _ in range(limit):
        reply = client.post("/api/lease", json={"worker": name}).get_json()
        if "unit" not in reply:
            break
        botornot.apply_settings(reply["settings"])
        client.sent_settings = reply["settings"]
        unit = reply["unit"]
        worker.NAME = name
        assert client.post("/api/report", json=worker.run(unit)).status_code == 200
        done.append(unit["kind"])
    return done


def add(client, target="@fake", **form):
    return client.post("/channels", data={"target": target, "every_hours": "0", **form})


def test_full_scan_of_a_channel(service):
    assert add(service).status_code == 302
    assert service.post("/channels/1/scan").headers["Location"].endswith("/scans/1")
    assert work(service) == ["channel", "comments", "comments", "card", "card"]

    page = service.get("/scans/1").get_data(as_text=True)
    assert "done" in page and "Spammer" not in page and "Spam" in page  # the card's name replaces the comment's
    assert "near-identical comments across videos" in page
    csv = service.get("/scans/1.csv").get_data(as_text=True).splitlines()
    assert csv[1].startswith(f"{botornot.W_CROSS_VIDEO_COPY},near-identical comments across videos (1),@spam")
    assert sum("UCowner" in line for line in csv) == 0  # the channel's own comments are not checked
    assert len(csv) == 1 + 2 + 1  # header, the spammer's two comments, the human's one

    for path in ("/", "/channels/1", "/accounts/UCspam", "/settings", "/healthz"):
        assert service.get(path).status_code == 200, path
    assert "Scans flagged in" in service.get("/channels/1").get_data(as_text=True)
    assert service.get("/accounts/UCnobody").status_code == 404


def test_meaning_check_is_off_unless_asked_for(service):
    add(service)
    add(service, target="@other", meaning="on")
    page = service.get("/").get_data(as_text=True)
    assert 'name="meaning" >' in page or 'name="meaning">' in page  # the add form's box starts unticked
    with orchestrator.db() as con:
        assert [row["meaning"] for row in con.execute("SELECT meaning FROM channels ORDER BY id")] == [0, 1]
    service.post("/channels/1/scan")
    work(service)
    assert "meaning check on" not in service.get("/scans/1").get_data(as_text=True)


def test_single_video_target_and_bad_links(service):
    assert "error=" in add(service, target="not a link").headers["Location"]
    add(service, target="https://www.youtube.com/watch?v=vid00000001")
    assert "error=" in add(service, target="vid00000001").headers["Location"]  # already in the list
    service.post("/channels/1/scan")
    assert work(service) == ["comments", "card", "card"]


def test_failed_cards_are_recorded_and_pause_the_queue(service):
    service.fake.fail_cards_of = ("UCspam", "UChuman")
    add(service)
    add(service, target="@second")
    service.post("/channels/1/scan")
    service.post("/channels/2/scan")
    work(service)
    with orchestrator.db() as con:
        cards = [row["data"] for row in con.execute("SELECT data FROM accounts")]
        assert orchestrator.get_state(con, "paused_until") > 0  # three cards failed in a row
    assert len(cards) == 3 and all("failed: HTTP 403" in c for c in cards)
    assert "Paused" in service.get("/").get_data(as_text=True)
    assert "wait" in service.post("/api/lease", json={"worker": "w1"}).get_json()
    service.post("/resume")
    assert work(service) == ["card"]  # the queue moves again


def test_rate_limit_report_pauses_everything(service):
    add(service)
    service.post("/channels/1/scan")
    unit = service.post("/api/lease", json={"worker": "w1"}).get_json()["unit"]
    result = worker.do(unit["kind"], unit["payload"])
    service.post("/api/report", json={"worker": "w1", "unit": unit["id"], "ok": True, "result": result, "rate_limited": 2})
    reply = service.post("/api/lease", json={"worker": "w1"}).get_json()
    assert "unit" not in reply and reply["wait"] > 0
    assert "rate-limited" in service.get("/").get_data(as_text=True)


def test_failed_unit_is_retried_then_fails_the_scan(service, monkeypatch):
    def broken(path):
        raise botornot.YouTubeError("the channel page failed: HTTP 404 (not found; check the link)")
    monkeypatch.setattr(service.fake, "channel", broken, raising=False)
    add(service)
    service.post("/channels/1/scan")
    assert work(service) == ["channel"] * orchestrator.UNIT_ATTEMPTS
    page = service.get("/scans/1").get_data(as_text=True)
    assert "failed" in page and "HTTP 404" in page
    log = service.get("/scans/1/log").get_json()
    assert log["errors"] == 2 and log["status"] == "failed"  # the unit's last failure, and the scan failing
    assert "2 error lines in this scan" in page  # the error banner is showing


def test_a_report_for_a_unit_the_worker_does_not_hold_is_ignored(service):
    add(service)
    service.post("/channels/1/scan")
    unit = service.post("/api/lease", json={"worker": "w1"}).get_json()["unit"]
    service.post("/api/report", json={"worker": "someone-else", "unit": unit["id"], "ok": True, "result": {}})
    with orchestrator.db() as con:
        assert con.execute("SELECT status FROM units WHERE id = ?", (unit["id"],)).fetchone()["status"] == "leased"


def test_settings_reach_the_workers_and_are_validated(service):
    form = {name: str(value) for name, value in orchestrator.DEFAULTS.items()}
    assert "saved=" in service.post("/settings", data={**form, "MAX_COMMENTS": "60", "W_CROSS_VIDEO_COPY": "9"}).headers["Location"]
    assert botornot.MAX_COMMENTS == 60 and botornot.W_CROSS_VIDEO_COPY == 9
    add(service)
    service.post("/channels/1/scan")
    work(service)
    assert service.sent_settings["MAX_COMMENTS"] == 60 and service.sent_settings["CARD_WAIT_MIN"] == 4.0
    assert service.get("/scans/1.csv").get_data(as_text=True).splitlines()[1].startswith("9,")

    for bad in ({"MAX_COMMENTS": "5"}, {"MAX_COMMENTS": "abc"}, {"SIMILARITY": "1.5"},
                {"CARD_WAIT_MIN": "9", "CARD_WAIT_MAX": "5"}):
        assert "error=" in service.post("/settings", data={**form, **bad}).headers["Location"], bad
    assert botornot.MAX_COMMENTS == 60  # a rejected form changes nothing

    botornot.MAX_COMMENTS = 200  # as after a restart: saved settings are read back from the database
    orchestrator.init_db()
    assert botornot.MAX_COMMENTS == 60
    service.post("/settings/reset")
    assert botornot.MAX_COMMENTS == orchestrator.DEFAULTS["MAX_COMMENTS"] == 200


def test_start_fresh_keeps_targets_and_settings(service):
    add(service)
    service.post("/channels/1/scan")
    work(service)
    form = {name: str(value) for name, value in orchestrator.DEFAULTS.items()}
    service.post("/settings", data={**form, "MAX_AUTHORS": "50"})
    assert "Deleted+1+scans" in service.post("/clean").headers["Location"]
    with orchestrator.db() as con:
        assert [con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("scans", "units", "accounts", "channels", "settings")] == [0, 0, 0, 1, 1]
    assert service.get("/").status_code == 200 and service.get("/scans/1").status_code == 404


def test_delete_one_scan_and_cancel(service):
    add(service)
    service.post("/channels/1/scan")
    work(service)
    service.post("/channels/1/scan")
    service.post("/scans/2/cancel")
    assert "cancelled" in service.get("/scans/2").get_data(as_text=True)
    assert work(service) == []  # nothing left to hand out
    service.post("/scans/1/delete")
    assert service.get("/scans/1").status_code == 404 and service.get("/scans/2").status_code == 200


def test_schedule_starts_due_scans(service):
    add(service, every_hours="6")
    with orchestrator.db() as con:
        channel = con.execute("SELECT * FROM channels").fetchone()
        assert channel["next_run"] <= orchestrator.time.time()  # a scheduled target is due at once
        orchestrator.start_scan(con, channel, "scheduled")
        later = con.execute("SELECT next_run FROM channels").fetchone()["next_run"]
    assert later == pytest.approx(orchestrator.time.time() + 6 * 3600, abs=5)
    service.post("/channels/1/edit", data={"every_hours": "0", "enabled": "on"})
    with orchestrator.db() as con:
        assert con.execute("SELECT next_run FROM channels").fetchone()["next_run"] is None


def test_review_links_go_to_the_exact_comment(service):
    add(service)
    service.post("/channels/1/scan")
    work(service)
    for path in ("/scans/1", "/accounts/UCspam"):
        page = service.get(path).get_data(as_text=True)
        assert 'href="https://www.youtube.com/watch?v=vid00000001&amp;lc=spam-vid00000001"' in page, path
        assert 'href="https://www.youtube.com/post/UgkxPost?lc=spam-UgkxPost"' in page, path
        assert page.count("part of a flagged pair") == 2, path  # the spammer's two comments, not the human's
    assert '<details data-key="UCspam" open>' in service.get("/scans/1").get_data(as_text=True)  # flagged accounts start unfolded
    assert '<details data-key="UCspam-1" open>' in service.get("/accounts/UCspam").get_data(as_text=True)


def test_site_lives_under_its_prefix(service):
    from werkzeug.test import Client
    site = Client(orchestrator.UnderPrefix(orchestrator.app, "/botornot"))
    assert site.get("/botornot/healthz").get_json() == {"ok": True}
    assert site.get("/").headers["Location"] == "/botornot/"
    assert site.get("/healthz").status_code == 404 and site.get("/botornotx/healthz").status_code == 404
    assert site.post("/botornot/resume").headers["Location"] == "/botornot/"  # links are built under the prefix
    assert Client(orchestrator.UnderPrefix(orchestrator.app, "")).get("/healthz").status_code == 200


def test_scan_log_and_progress(service):
    add(service)
    service.post("/channels/1/scan")
    first = service.get("/scans/1/log").get_json()
    assert first["status"] == "running" and first["progress"] == 2 and first["doing"] == "listing videos and posts"
    assert [l["message"] for l in first["lines"]] == ["scan of @fake started (started by hand)"]

    work(service, limit=1)  # the channel unit
    assert service.get("/scans/1/log").get_json()["progress"] == 5
    work(service, limit=2)  # both comments units
    assert service.get("/scans/1/log").get_json()["progress"] == 25
    work(service)
    log = service.get("/scans/1/log").get_json()
    assert log["status"] == "done" and log["progress"] == 100 and log["errors"] == 0 and log["checked"] == 2
    sources = {l["source"] for l in log["lines"]}
    assert sources == {"orchestrator", "w1"}  # what the orchestrator decided, and what the worker logged
    assert any(l["level"] == "DEBUG" and "received" in l["message"] for l in log["lines"])
    assert any("unit 1 (channel) done" in l["message"] for l in log["lines"])

    newer = service.get(f"/scans/1/log?after={log['lines'][-2]['id']}").get_json()["lines"]
    assert len(newer) == 1 and newer[0]["message"].startswith("done after")
    text = service.get("/scans/1/log.txt").get_data(as_text=True)
    assert text.count("\n") == len(log["lines"]) and "[orchestrator] scan of @fake started" in text

    page = service.get("/scans/1").get_data(as_text=True)
    assert 'id="log"' in page and 'aria-valuenow="100"' in page and "error line" not in page
    assert 'role="progressbar"' in service.get("/").get_data(as_text=True)
    service.post("/scans/1/delete")
    with orchestrator.db() as con:
        assert con.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == 0  # a deleted scan takes its log with it


def test_pages_keep_themselves_current(service):
    """Every page names its live parts and how often to refresh; a running scan refreshes faster."""
    add(service)
    service.post("/channels/1/scan")
    page = service.get("/scans/1").get_data(as_text=True)
    assert f'<body data-refresh="{orchestrator.LIVE_BUSY}">' in page and "location.reload" not in page
    assert 'id="live-scan" data-live' in page and 'id="live-results" data-live' in page
    assert "No accounts checked yet" in page and f'data-refresh="{orchestrator.LIVE_BUSY}"' in service.get("/").get_data(as_text=True)
    work(service, limit=4)  # channel, both comments units, and the first card
    page = service.get("/scans/1").get_data(as_text=True)
    assert page.count("<details") == 1 and "<b>1 / 2</b>" in page  # the same address now shows the first account
    work(service)
    page = service.get("/scans/1").get_data(as_text=True)
    assert page.count("<details") == 2 and 'data-key="UCspam" open' in page and "<b>2 / 2</b>" in page
    assert f'<body data-refresh="{orchestrator.LIVE_SETTLED}">' in page  # finished: slower
    for path, parts in (("/", ("live-status", "live-targets", "live-scans", "live-workers")),
                        ("/channels/1", ("live-channel", "live-scans", "live-accounts")),
                        ("/accounts/UCspam", ("live-account",)), ("/settings", ("live-counts", "live-events"))):
        page = service.get(path).get_data(as_text=True)
        assert all(f'id="{part}" data-live' in page for part in parts) and "data-refresh=\"" in page, path
        assert len(re.findall(r'id="[\w-]+" data-live', page)) == page.count(" data-live>") == len(parts), path  # all named


def test_activity_record_says_who_did_what(service):
    add(service)
    service.post("/channels/1/edit", data={"every_hours": "6", "enabled": "on"})
    service.post("/channels/1/scan")
    work(service)
    form = {name: str(value) for name, value in orchestrator.DEFAULTS.items()}
    service.post("/settings", data={**form, "MAX_AUTHORS": "50"})
    service.post("/scans/1/delete")
    service.post("/clean")
    service.post("/channels/1/delete")
    with orchestrator.db() as con:
        events = [(e["who"], e["message"]) for e in con.execute("SELECT * FROM events ORDER BY id")]
    assert all(who == "127.0.0.1" for who, _ in events)
    assert [m.split(":")[0].split(" (")[0] for _, m in events] == [
        "added channel @fake, schedule", "changed @fake", "started scan 1 of @fake",
        "changed the setting MAX_AUTHORS from 100 to 50", "deleted scan 1",
        "deleted all 0 scans and their results", "deleted the target @fake and its 0 scans"]
    page = service.get("/settings").get_data(as_text=True)
    assert "deleted the target @fake" in page and "Recent activity" in page


def test_ids_are_never_reused_and_old_databases_are_converted(service, tmp_path, monkeypatch):
    add(service)
    service.post("/channels/1/scan")
    service.post("/channels/1/delete")
    add(service, target="@second")
    assert service.post("/channels/2/scan").headers["Location"].endswith("/scans/2")  # not channel 1 / scan 1 again
    assert service.post("/channels/1/scan").status_code == 404

    import sqlite3
    old = str(tmp_path / "old.db")
    con = sqlite3.connect(old)
    con.executescript(orchestrator.SCHEMA.replace(" AUTOINCREMENT", ""))
    con.execute("INSERT INTO channels (id, target, kind, created) VALUES (7, '@kept', 'channel', 1)")
    con.execute("INSERT INTO scans (id, channel_id, status, stage, started) VALUES (9, 7, 'done', 'cards', 1)")
    con.commit(); con.close()
    monkeypatch.setattr(orchestrator, "DB", old)
    orchestrator.init_db()
    assert "@kept" in service.get("/").get_data(as_text=True) and service.get("/scans/9").status_code == 200
    service.post("/channels/7/delete")
    add(service, target="@new")
    with orchestrator.db() as con:
        assert con.execute("SELECT id FROM channels").fetchone()["id"] == 8


def test_posts_from_other_sites_are_refused(service):
    add(service)
    assert service.post("/clean", headers={"Origin": "https://evil.example"}).status_code == 403
    assert service.post("/channels/1/delete", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert service.post("/api/lease", json={"worker": "x"}, headers={"Origin": "null"}).status_code == 403
    assert service.get("/", headers={"Origin": "https://evil.example"}).status_code == 200  # reading is harmless
    assert service.post("/channels/1/delete", headers={"Origin": "http://localhost"}).status_code == 302  # our own page


def test_only_live_workers_are_listed(service):
    add(service)
    service.post("/channels/1/scan")
    now = orchestrator.time.time()
    with orchestrator.db() as con:  # two workers from before a redeploy: one long gone, one seen two minutes ago
        con.execute("INSERT INTO workers (name, last_seen, units_done) VALUES ('dead-container', ?, 40), ('recently-gone', ?, 7)",
                    (now - 7200, now - 120))
    service.post("/api/lease", json={"worker": "idle-one"})           # takes the channel unit ...
    with orchestrator.db() as con:                                    # ... and has been busy with it for five minutes
        con.execute("UPDATE workers SET last_seen = ? WHERE name = 'idle-one'", (now - 300,))
        con.execute("UPDATE workers SET name = 'busy-one' WHERE name = 'idle-one'")
        con.execute("UPDATE units SET worker = 'busy-one' WHERE worker = 'idle-one'")
    service.post("/api/lease", json={"worker": "fresh-one"})          # nothing for it to do, but it just asked
    page = service.get("/").get_data(as_text=True)
    assert "<b>2</b><span class=\"muted small\">workers online" in page
    assert "fresh-one" in page and "waiting for work" in page
    assert "busy-one" in page and "working on a channel unit of" in page  # silent while it works, but not dead
    assert "dead-container" not in page and "recently-gone" not in page
    with orchestrator.db() as con:  # the hourly clean-up drops the one long gone
        con.execute("DELETE FROM workers WHERE last_seen < ?", (now - orchestrator.WORKER_FORGOTTEN,))
        assert {w["name"] for w in con.execute("SELECT name FROM workers")} == {"recently-gone", "busy-one", "fresh-one"}
