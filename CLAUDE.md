# YouTube Bot Detection

Goal: judge whether a YouTube commenter is a bot, using the comments they leave.

## Files
- `botornot.py` – the scanner and the command-line tool. `python botornot.py <channel>` (`@SomeChannel` or any link to the channel) scans the channel's newest videos and posts and writes `<@handle>.csv`. A single video or post link also works and writes `<video or post ID>.csv`. `--meaning` adds the meaning check, `--verbose` logs every request and wait.
- `orchestrator.py`, `worker.py`, `templates/` – the service: web page, schedules and a work queue (see The service below). They import `botornot.py`.
- `requirements.txt` – `requests`, all the command-line tool needs (installed in `.venv`; rebuild with `python -m venv .venv` then `.venv/bin/pip install -r requirements.txt`).
- `requirements-service.txt` – flask and waitress, for `orchestrator.py` only.
- `requirements-meaning.txt` – extra packages for `--meaning` only (sentence-transformers, transformers, torch, torchvision, pillow; installed in `.venv` as CPU builds, which makes it about 1.4 GB). The model itself lives in `~/.cache/huggingface` (about 1.5 GB).
- `Dockerfile`, `.dockerignore`, `compose.yaml` – the images and how the service's containers run together.
- `ansible/deploy.yml` (deploy), `ansible/manage.yml` (status, logs, restart, stop, start, clean, teardown, purge) and `ansible/vars.yml` (what both share) – for the `docker_servers` inventory group; see The service below. `ansible/inventory.example.ini` is a template; the real inventory is `ansible/inventory.ini` (git ignores it) or one kept outside the repo.
- `tests/` – pytest suite (about a second, no network: every YouTube reply is faked; `tests/fixtures/profile_card.json` is made up). `requirements-dev.txt` is pytest.
- `.github/workflows/ci.yml` – GitHub Actions on every push: the tests on Python 3.14 (the image's version), a build of the small image, and a syntax check of both playbooks.
- `README.md`, `LICENSE` (GPL-3.0-or-later), `.gitignore`.
- `<@handle>.csv`, `<ID>.csv` – scan results. Each run overwrites the file for that channel, video or post.

## How it works (all anonymous: no API key, no cookies; the command-line tool stores nothing between runs except the downloaded `--meaning` model)
0. Channel only: the `/videos` and `/posts` tabs of the channel → newest video IDs (`lockupViewModel`, `contentId`) and post IDs (`backstagePostRenderer`, `postId`), plus the channel ID (`channelMetadataRenderer.externalId`). Steps 1–2 then run once per video/post, `WORKERS` (4) at a time in threads, each on its own session, and the commenters are merged in list order. Cards (step 3) are fetched `CARD_WORKERS` (4) at a time, each worker on its own session and waiting `CARD_WAIT` between its own requests. One further background thread loads the `--meaning` model while comments are read, then scores each account while cards are still coming in.
1. Video or post page → `INNERTUBE_CLIENT_VERSION`, owner channel (`videoOwnerRenderer` for a video, `backstagePostRenderer` for a post), comment-section continuation token.
2. `youtubei/v1/next` (video) or `youtubei/v1/browse` (post) with that token → newest top-level comments (`commentEntityPayload`, `replyLevel` 0), paged 20 at a time with a 1–2 s pause; duplicates skipped.
3. Per commenter (once, however many videos/posts they commented on): `youtubei/v1/account/get_profile_card`, token = (owner channel, the commenter's own comment ID). When there are more commenters than the limit, those seen on the most videos/posts are checked first.
4. Score and write the CSV: one row per comment (the commenter's comments on the scanned videos/posts plus those on their card, with links), account columns repeated, highest-scoring accounts first.

Limits and weights are constants at the top of `botornot.py`: 5 videos and 5 posts per channel, 200 comments per video/post, 100 authors per run, 4–7 s between one card worker's requests (`CARD_WAIT`; with 4 workers that is a card about every 1.4 s), 1–2 s between comment pages (`PAGE_WAIT`), back-off on every request, pages included (`fetch()`): the wait doubles per retry from `BACKOFF` (4 s), each up to 1.5x longer at random. Rate-limiting (HTTP 429) gets 5 retries (4, 8, 16, 32, 64 s; about 2.5 minutes in all) and never waits less than YouTube's `Retry-After`; a `Retry-After` over 5 minutes ends the request at once. Network errors and HTTP 403/5xx get 2 retries. Every wait is random within its range: a normal distribution centred on the middle, redrawn if outside (`pause()`).

Scoring: near-identical comments (ratio 0.90+, texts of 25+ characters) across videos +3, on the same video or post +2, account younger than about 5 weeks +1. `max_similarity` is the closest pair of comments for that account, flagged or not.

`--meaning` (optional) adds: reworded comments that say the same thing +2 (`W_SAME_MEANING`). Each account's comments of 25+ characters are embedded with `google/embeddinggemma-2` (`MEANING_MODEL`; text part only, prompt `SentenceSimilarity`) and a pair counts when its cosine similarity is 0.90+ (`MEANING_SIMILARITY`) and the text check did not already catch it. `max_meaning_similarity` is the closest pair, flagged or not (empty without `--meaning`). It is loaded in full precision (float32) on a CPU, which is about 5x faster there than the half precision it is stored in. The model runs locally: comments are not sent anywhere. Without the flag nothing extra is imported.

Free-text cells that start with `= + - @` get a leading `'` so a spreadsheet will not run a stranger's comment as a formula.

Failures are reported, not hidden: page/comment errors end the run with an `ERROR` line and exit code 1; a failed card is recorded in the `card` column, and 3 failed cards in a row stop the run (what was collected is still written).

Logging: everything goes through Python's `logging` (logger `botornot`, with `botornot.orchestrator` and `botornot.worker` under it) to stderr, as `time LEVEL [thread] message`. INFO is progress, WARNING is retries, skipped items and failed cards, ERROR ends a run or a unit, DEBUG (`--verbose` or `BOTORNOT_LOG=DEBUG`) adds every request with status, size and time, and every wait.

## The service
- Web page: `http://<server>:8080/botornot/` (everything, the worker API included, is under `/botornot`; `/` redirects there; `BOTORNOT_PREFIX` changes it) – add channels (or single videos/posts), give each a schedule (manual, hourly … weekly) and the meaning check on or off, start a scan by hand, watch it (progress bar and live log), read results, download a scan's CSV, delete a scan. For review, the comments that formed a flagged pair are marked (`matched`, set by `score()`) and every comment has an "Open this comment on YouTube" link (`watch?v=…&lc=…` or `/post/…?lc=…`), where a person can report it from the comment's ⋮ menu. The tool itself reports nothing. Per channel it lists scans over time and the accounts that scored above 0 in any scan; per account, every scan it appeared in. No login: home network only.
- `orchestrator.py` (one container, image `botornot`): the web page, the scheduler, and a queue of units in SQLite (`/data/botornot.db` in the `botornot-data` volume), which also holds results, settings and each scan's log. A scan is cut into units: `channel` (list the tabs) → one `comments` unit per video/post → one `card` unit per commenter (fetch the card and score).
- `worker.py` (4 containers, image `botornot:meaning`): each asks the orchestrator for one unit (`POST /api/lease`), does it, reports back (`POST /api/report`). Workers are all alike and keep nothing; a unit not reported within 15 minutes is handed out again, a failed unit is tried twice.
- The orchestrator sets the pace, not the workers: `card` units leave at the same rate as the command-line tool (`CARD_WAIT` / `CARD_WORKERS`), at most `WORKERS` other units are out at once, and after a reported HTTP 429 or 3 failed cards in a row nothing is handed out for 1 minute, doubling to an hour at most ("Paused" banner on the page, with a Resume button). So adding worker containers does not raise the request rate; every worker reaches YouTube from the same address anyway.
- Every page keeps itself current without a reload (script in `templates/base.html`): it re-fetches its own address every 3 s while a scan runs, 5 s otherwise, 15 s for finished scans and account pages (`LIVE_BUSY`, `LIVE_IDLE`, `LIVE_SETTLED`), and replaces each element marked `data-live` (they need an `id`) with the fresh version. A part is skipped while a form in it has unsaved edits, and `<details data-key>` keep their folded state. Anything new that shows data must sit inside a `data-live` part. Checked on 2026-10-10 in headless Chrome against a local instance with fake workers (dashboard, channel, scan, settings pages all updated with no reload).
- Safety nets: target and scan IDs are never reused (`AUTOINCREMENT`; `migrate()` converts an older database), so a stale page cannot act on a different target. Every add, change, start, cancel and delete made on the web page is kept in the `events` table with the address it came from and shown under "Recent activity" on the settings page; before that, deletions were only in the container log, which a redeploy discards (on 2026-10-10 a server's targets vanished and nobody could say how). A POST whose `Origin` is another site is refused (`own_pages_only()`), since the site has no login.
- Settings page (`/settings`): how much to read (`MAX_VIDEOS`, `MAX_POSTS`, `MAX_COMMENTS`, `MAX_AUTHORS`), every scoring weight and threshold, and the pace (`CARD_WORKERS`, `CARD_WAIT`, `WORKERS`, `PAGE_WAIT`), each with bounds (`SETTINGS` in `orchestrator.py`). Changes are stored in the database, applied to the orchestrator at once, and sent to workers with every unit (`botornot.apply_settings`, which only accepts names in `botornot.TUNABLE`). The constants in `botornot.py` are the defaults. "Delete all scans" on the same page starts fresh: scans, units and results go, targets, schedules and settings stay.
- The meaning check is per target and off unless ticked when adding or editing it (and `--meaning` on the command line). Workers load the model only when a unit asks for it.
- Each scan has its own log (`logs` table): what the orchestrator decided (`scan_log()`), plus every line a worker logged while doing one of its units, DEBUG included (`worker.run()` captures them and sends them with the report, whatever level the container itself prints at). The scan page polls `/scans/<id>/log?after=N` every 2 s for new lines (polling, not a held-open stream, so it does not tie up server threads) and filters by level in the browser; a red banner shows while the log has ERROR lines and an amber one for WARNING lines; `/scans/<id>/log.txt` is the raw text. Progress is 5% listing, 20% reading comments, 75% profile cards (`progress()`).
- Deploy: `ansible-playbook -i <inventory> ansible/deploy.yml` copies the source to `~/botornot` on the server, builds both images there and restarts the containers (`-e botornot_workers=N`, `botornot_port`, `botornot_log`). Afterwards: `ansible/manage.yml -e do=status|logs|restart|stop|start|clean|teardown|purge` (`purge` deletes data and needs `-e confirm=yes`; it has only been run with `--check`). The images carry the label `app=botornot` so `clean` prunes only their old builds; it also empties Docker's build cache, which is server-wide.
- The server it has run on is a small VM (4 CPUs, 16 GB RAM). Each worker that has used the meaning check holds about 1.9 GB. Its disk is slow at flushing: the test suite took 97 s there against 0.3 s on a laptop, which is why the database runs with `PRAGMA synchronous = NORMAL`.
- No Kubernetes: one machine and one outgoing address, so Compose does the same job with far less to run. Kubernetes would start to pay off with several machines or several outgoing addresses.
- A single scan without the service: `docker run --rm -v botornot-data:/data botornot @SomeChannel` (CSV lands in the volume; fetch it with `docker run --rm -v botornot-data:/data --entrypoint cat botornot /data/@SomeChannel.csv > @SomeChannel.csv`).

## Findings (from runs in this project, 2026-10-10)
- `get_profile_card` works **anonymously** with a minimal body: `profileOwnerExternalChannelId`, `profileCardContext`, `context.client` (WEB, clientVersion, hl, gl). No visitorData needed; a two-year-old clientVersion still worked.
- `profileCardContext` = base64url protobuf: tag bytes `82f5dcc306`, length, field 2 = owner channel ID, field 3 = comment ID, optional field 6 = varint 1 (browser tokens only; not needed).
- The **owner channel in the token** decides whether the comment list and comment-count badge appear. Comment IDs are not validated.
- Comments are scoped to the owner channel ("On this channel"), NOT all of YouTube. The list holds at most 3; the badge counts up to "100+".
- Response: find `profileCardViewModel` anywhere in the JSON (identity info + `comments[]` + `profileBadgeInfoChannelWide`).
- Join text looks like "Joined 3 weeks ago" (with `hl=en`).
- Comments load anonymously. yt-dlp is not needed (its player request hits "Sign in to confirm you're not a bot") and is no longer installed.
- `google/embeddinggemma-2` (Apache 2.0, not gated, no Hugging Face account needed): loads text-only in about 10 s on CPU, 1.3 GB RAM in half precision, about 2 GB in full. In half precision on a CPU 6 short texts took 10 s; in full precision 2 s, and the calibration below came out the same (the same 7 pairs). Its processor refuses to load without torchvision and pillow even for text.
- Its similarity scale is compressed: unrelated comments score about 0.75, same topic with a different point 0.75–0.88, rewordings 0.92–0.99, a Spanish translation 0.96. On 320 same-account pairs from 40 commenters on a large news channel, 7 pairs under the 0.90 text ratio scored 0.90+ and all 7 were rewordings; pairs at 0.89–0.90 were mixed (shared template or shared topic).
- Community posts (`/post/Ug...`, or `community?lb=Ug...`): cards work with a post comment ID in the token. The card still lists only video comments (each has a `videoId`).
- Channel tabs load anonymously: `/videos` lists 30 videos, `/posts` listed 9 posts (first page only; further pages are not fetched). A channel without a tab answers with its home tab, so the selected tab's URL is checked.
- Now and then a video page arrives without `ytInitialData` (seen once in about 60 page loads on 2026-10-10; the same page loaded fine 6 times straight after). `page()` fetches it again up to twice before giving up.
- Post comment paging works (73 top-level comments over 4 pages). The comment count YouTube states includes replies.
- 100 profile cards at 4 at a time (about one every 1.4 s), from one address: all returned, no HTTP 429. That is the highest rate and the largest run so far.

## Scans so far (2026-10-10)
- A video on a commentary channel, 30 of 47 commenters: nobody flagged; looked like genuine discussion.
- A post on a large news channel, all 9–10 commenters: two accounts scored 3 for pasting the same long text on the post and on 1–3 of the channel's videos. Both are 13–15 years old with "100+ comments", so copy-paste spammers, not fresh accounts.
- A large news channel, test with small limits (2 videos, 2 posts, 40 comments each, 8 of 75 commenters): one account scored 3 for near-identical comments across videos.
- A large news channel with `--meaning`, 12 of 213 commenters (3 videos, 2 posts): three accounts scored 3 for cross-video copy-paste, and one scored 2 on meaning alone (text similarity 0.68, meaning 0.95, three reworded pairs).
- Regression pass of the command-line tool on 2026-10-10 (after logging, concurrent cards, settings and the service were added): A post on a large news channel gave the same two accounts at score 3 as the first scan of it; a single video 48 of 48 cards in 70 s; a full channel scan with `--meaning` 100 of 100 cards in 169 s, 28 scored above 0 (one of its 10 videos/posts was skipped by the missing-page-data glitch above).
- A post on a large news channel, all 73 commenters (largest run; all cards returned, about 5 minutes): one account scored 1 for being a month old; no copy-paste hits.
- The same channel through the service, full size with the meaning check: 10 videos/posts, 575 commenters, 100 checked in 213 s, no failed cards. 26 scored above 0: 12 for copy-paste across videos (3 of them also reworded), 12 on meaning alone (closest pairs 0.90–0.98), 2 for a very new account. The 100 checked are the most active commenters, who have the most pairs to compare, so the meaning-only ones need a human look before the 0.90 threshold is trusted.

## Untested / open
- Scoring weights are untuned; the only signal confirmed on real accounts is cross-video copy-paste.
- Idea not yet built: comments-per-account-age from the badge (a 3-month-old account had "100+ comments" on one channel).
- `MEANING_SIMILARITY` 0.90 comes from one calibration (see Findings) and is not tuned against confirmed bots. Comments compared are one account's own; comparing across accounts (many accounts posting the same reworded line) is not built.
- Whether YouTube throttles above 4 comment readers or 4 card workers is unknown.
- The service has run two full scans. Untested on the real thing (the test suite covers them with a faked YouTube): scheduled scans firing, the pause after rate-limiting, a worker dying mid-unit, two scans queued at once, and the red error banner on a real failed scan.
- The database keeps every scan forever; with its log (a 100-commenter scan logs several hundred lines); clean-up is by hand (delete a scan, or all of them, on the web page).
- A video on the channel's tab that another channel owns would still get cards scoped to the scanned channel (not seen yet).
- Replies are skipped; a lot of spam lives in replies.
- Whether the card's comment list is stable between runs is unknown.
- No run has gone past 100 cards, and none has been rate-limited, so where YouTube's limit sits is unknown and the 429 back-off is tested only against faked responses.

## Rules
- Undocumented endpoints: keep the waits and the back-off, and raise `MAX_AUTHORS` or `CARD_WORKERS`, or lower `CARD_WAIT`, with care. Mention ToS risk when relevant.
- After any change to the source (`botornot.py`, `orchestrator.py`, `worker.py`, `templates/`, requirements, `Dockerfile`, `compose.yaml`), run the tests (`.venv/bin/python -m pytest tests`) and then the deploy playbook: the deployed containers run a copy of the code, so they stay on the old version until then.
- A deployed service holds real targets and scan results. Never test against it by adding, scanning or deleting targets: on 2026-10-10 a test script picked "the first target in the list", scanned a real target and then deleted it with its results. Test on a local orchestrator with a temporary database (`BOTORNOT_DB=...`), and on a deployed server only read.
- The repo is meant to be published: never commit scan results, the database, a saved reply about a real commenter, the real Ansible inventory, or anything naming a real commenter.
- Any HAR or cURL capture from a logged-in browser contains live session credentials. Never commit or share one.
- The output CSVs and the service's database contain other people's comments; keep them local, and keep the web page off the internet.

## Next steps
1. Add channels with suspected bots on the web page and review the results.
2. Tune scoring against accounts you can confirm as bots or humans.
