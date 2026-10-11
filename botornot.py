#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Caleb Rollins
"""
Score the commenters of a YouTube channel's newest videos and community posts for bot-like signals.

  python botornot.py @SomeChannel                              (or the channel's link)
  python botornot.py https://www.youtube.com/watch?v=VIDEO_ID
  python botornot.py https://www.youtube.com/post/POST_ID
  python botornot.py @SomeChannel --meaning                    (also catch reworded comments)
  python botornot.py @SomeChannel --verbose                    (log every request and wait)

A channel is scanned through its Videos and Posts tabs: the newest few of each (limits below),
commenters merged across them. A video or post link scans just that one.

Writes <@handle, video ID or post ID>.csv: one row per comment (the commenter's comments on
the scanned videos/posts plus the ones on their profile card), highest-scoring accounts first.

Needs only `requests` (pip install -r requirements.txt). Everything is fetched anonymously
and nothing is stored between runs: no API key, no cookies, no tokens. The client version
is read from the page on every run.

--meaning compares what comments say, not only their wording, with a local embedding model
(MEANING_MODEL below). It needs the packages in requirements-meaning.txt and downloads the
model once (about 1.5 GB, no account or key). The model runs on this machine: comments are
not sent anywhere.

Top-level comments only (replies are skipped), newest first, up to the limits
set at the top of this file (5 videos, 5 posts, 200 comments each, 100 commenters in all).
Comments of several videos/posts are read at the same time (WORKERS threads), profile cards
are requested CARD_WORKERS at a time, and one more thread loads the --meaning model and scores
accounts meanwhile. Every wait is a random time within a set range.

Progress is logged to stderr with a level (DEBUG, INFO, WARNING, ERROR); --verbose shows DEBUG,
or set BOTORNOT_LOG to a level name.
Problems are never silent: one that ends the run is logged as ERROR (exit code 1), a commenter
whose profile card could not be fetched is logged as WARNING and noted in the CSV's `card` column.

The endpoints are YouTube's internal ones (youtubei/v1/next, browse, account/get_profile_card).
They are undocumented, can change at any time, and automated use may violate YouTube's
Terms of Service. Scores are heuristics, not verdicts.
"""
import base64
import csv
import difflib
import itertools
import json
import logging
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("botornot")

ORIGIN = "https://www.youtube.com"
FALLBACK_CLIENT_VERSION = "2.20261009.01.00"  # only used if the page does not state one

MAX_VIDEOS = 5             # newest videos to read from a channel's Videos tab
MAX_POSTS = 5              # newest posts to read from a channel's Posts tab
WORKERS = 4                # videos/posts whose comments are read at the same time
MAX_COMMENTS = 200         # newest top-level comments to read per video or post (20 per request)
MAX_AUTHORS = 100          # commenters to check in all (one profile-card request each)
CARD_WORKERS = 4           # profile cards requested at the same time; each worker waits CARD_WAIT between its own
# Waits are random within (shortest, longest) seconds, bell-shaped around the middle - see wait_time()
CARD_WAIT = (4.0, 7.0)     # between one card worker's requests (so about CARD_WORKERS cards per 5.5 s in all)
PAGE_WAIT = (1.0, 2.0)     # between comment pages, and between a channel's tabs
BACKOFF = 4.0              # shortest wait before a retry; doubles for each retry after it
RATE_LIMIT_RETRIES = 5     # retries when YouTube rate-limits (HTTP 429): 4, 8, 16, 32, 64 s or a bit more
RETRIES = 2                # retries for other passing failures (network, HTTP 403/500/502/503)
LONGEST_WAIT = 300         # give up at once if YouTube asks for a longer wait than this (Retry-After)

# Scoring (untuned)
SIMILARITY = 0.90          # two comments count as "the same text" at or above this ratio (0 to 1)
MIN_TEXT_LEN = 25          # ignore shorter comments ("first!", "nice") - they match by chance
W_CROSS_VIDEO_COPY = 3     # near-identical comments on different videos (or a post and a video)
W_REPEAT_ON_VIDEO = 2      # near-identical comments on the same video or post
W_NEW_ACCOUNT = 1          # account younger than NEW_ACCOUNT_YEARS
NEW_ACCOUNT_YEARS = 0.1    # about 5 weeks
W_SAME_MEANING = 2         # reworded comments that say the same thing (only with --meaning)
MEANING_MODEL = "google/embeddinggemma-2"  # Hugging Face embedding model, text part only (270M parameters)
MEANING_SIMILARITY = 0.90  # two comments "say the same thing" at or above this cosine similarity (0 to 1);
                           # this model gives unrelated comments about 0.75, so the usable range is narrow


# the constants above that the service's settings page may change (see apply_settings)
TUNABLE = ("MAX_VIDEOS", "MAX_POSTS", "MAX_COMMENTS", "MAX_AUTHORS", "WORKERS", "CARD_WORKERS", "CARD_WAIT", "PAGE_WAIT",
           "SIMILARITY", "MIN_TEXT_LEN", "W_CROSS_VIDEO_COPY", "W_REPEAT_ON_VIDEO", "W_NEW_ACCOUNT", "NEW_ACCOUNT_YEARS",
           "W_SAME_MEANING", "MEANING_SIMILARITY")


def apply_settings(values):
    """Set constants at the top of this file from {name: value}. Only names in TUNABLE are taken;
    a range such as CARD_WAIT is given as CARD_WAIT_MIN and CARD_WAIT_MAX."""
    for name, value in values.items():
        base, end = (name[:-4], name[-3:]) if name.endswith(("_MIN", "_MAX")) else (name, None)
        if base not in TUNABLE or isinstance(globals()[base], tuple) != bool(end):
            log.warning("ignored unknown setting %s", name)
        elif end:
            shortest, longest = globals()[base]
            globals()[base] = (value, longest) if end == "MIN" else (shortest, value)
        else:
            globals()[base] = value


def wait_time(shortest, longest):
    """A random time in the range, in seconds: normally distributed around the middle (the range
    is 2 standard deviations either side), drawn again if it lands outside."""
    if longest <= shortest:
        return shortest
    middle, spread = (shortest + longest) / 2, (longest - shortest) / 4
    while True:
        seconds = random.normalvariate(middle, spread)  # normalvariate is safe across threads
        if shortest <= seconds <= longest:
            return seconds


def pause(shortest, longest):
    """Sleep for a wait_time() in the range."""
    seconds = wait_time(shortest, longest)
    log.debug("waiting %.1f s", seconds)
    time.sleep(seconds)


def dig(obj, *path, default=None):
    for p in path:
        try:
            obj = obj[p]
        except (KeyError, IndexError, TypeError):
            return default
    return obj


def find_all(obj, key):
    """Every value stored under `key` anywhere in a nested JSON structure, in document order."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from find_all(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_all(v, key)


def find_key(obj, key):
    return next(find_all(obj, key), None)


class YouTubeError(Exception):
    """YouTube refused, blocked, or answered in a shape this script does not understand."""


class YouTube:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.s.cookies.set("SOCS", "CAI", domain=".youtube.com")  # skip the consent page (some regions)
        self.client_version = FALLBACK_CLIENT_VERSION
        self.comments_api = "next"  # videos page their comments through "next", posts through "browse"
        self.rate_limited = 0       # HTTP 429 replies seen so far, including ones a retry got past

    def fetch(self, what, method, url, **kwargs):
        """A request that is sent again after a wait when YouTube rate-limits it or fails for a
        moment. The wait doubles with every retry (BACKOFF, then 2x, 4x ...) and is never shorter
        than the Retry-After YouTube states. Returns the HTTP 200 response or raises YouTubeError."""
        retry = 0
        while True:
            limited, asked, started = False, 0, time.monotonic()
            try:
                r = self.s.request(method, url, timeout=20, **kwargs)
            except requests.RequestException as e:
                problem = f"network error ({type(e).__name__})"
            else:
                log.debug("%s %s -> HTTP %s, %d bytes in %.2f s", method, what, r.status_code,
                          len(r.content), time.monotonic() - started)
                if r.status_code == 200:
                    return r
                problem = f"HTTP {r.status_code}" + {403: " (blocked)", 404: " (not found; check the link)",
                                                     429: " (rate-limited)"}.get(r.status_code, "")
                if r.status_code not in (403, 429, 500, 502, 503):
                    break
                limited = r.status_code == 429
                self.rate_limited += limited
                after = r.headers.get("Retry-After", "")
                asked = int(after) if after.isdigit() else 0
            if asked > LONGEST_WAIT:
                problem += f"; YouTube asks for a {asked} s wait"
                break
            if retry >= (RATE_LIMIT_RETRIES if limited else RETRIES):
                break
            wait = max(BACKOFF * 2 ** retry, asked)
            retry += 1
            log.warning("%s: %s; waiting about %.0f s (retry %d of %d)", what, problem, wait * 1.25, retry,
                        RATE_LIMIT_RETRIES if limited else RETRIES)
            pause(wait, wait * 1.5)
        raise YouTubeError(f"{what} failed: {problem}")

    def post(self, path, **body):
        """Innertube call (with fetch's back-off). Raises YouTubeError."""
        body["context"] = {"client": {"hl": "en", "gl": "US", "clientName": "WEB",
                                      "clientVersion": self.client_version}}
        r = self.fetch(f"{path} request", "POST", f"{ORIGIN}/youtubei/v1/{path}",
                       params={"prettyPrint": "false"}, json=body)
        try:
            return r.json()
        except ValueError:
            raise YouTubeError(f"{path} request failed: a reply that is not JSON (possibly a bot check)")

    def page(self, url, what):
        """Load a YouTube page (with fetch's back-off) and return its ytInitialData.
        Picks up the client version on the way."""
        for attempt in range(RETRIES + 1):
            r = self.fetch(f"the {what} page", "GET", url)
            if "consent." in r.url or "/sorry/" in r.url:
                raise YouTubeError(f"YouTube redirected to a consent or bot-check page ({r.url.split('?')[0]})")
            m = re.search(r"ytInitialData\s*=\s*", r.text)
            if m:
                break
            if attempt < RETRIES:  # now and then YouTube serves a page without it; the next try usually has it
                log.warning("the %s page came without its data; fetching it again (retry %d of %d)", what,
                            attempt + 1, RETRIES)
                pause(BACKOFF, BACKOFF * 1.5)
        else:
            raise YouTubeError(f"the {what} page has no ytInitialData; the page format has probably changed")
        version = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', r.text)
        if version:
            self.client_version = version.group(1)
        try:
            data, _ = json.JSONDecoder().raw_decode(r.text, m.end())
        except ValueError:
            raise YouTubeError(f"could not read the {what} page data; the page format has probably changed")
        return data

    def channel(self, path):
        """Newest videos and posts of a channel, read from its Videos and Posts tabs (`path` is
        "@handle", "channel/UC...", "c/name" or "user/name").
        Return (channel_id, name for the output file, [(kind, item_id, label)])."""
        items = []
        for tab, kind, limit in (("videos", "video", MAX_VIDEOS), ("posts", "post", MAX_POSTS)):
            data = self.page(f"{ORIGIN}/{path}/{tab}", "channel")
            meta = find_key(data, "channelMetadataRenderer") or {}
            if not meta.get("externalId"):
                raise YouTubeError("no channel found at that link; it may be removed or mistyped")
            # a channel without this tab answers with its home tab instead
            selected = [t for t in find_all(data, "tabRenderer") if t.get("selected")
                        and (find_key(t.get("endpoint"), "url") or "").endswith("/" + tab)]
            if kind == "video":
                found = [(v.get("contentId"), dig(v, "metadata", "lockupMetadataViewModel", "title", "content"))
                         for v in find_all(selected, "lockupViewModel")
                         if v.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO" and v.get("contentId")]
            else:
                found = [(p["postId"], None) for p in find_all(selected, "backstagePostRenderer")
                         if p.get("postId")]
            log.info("%s tab of %s: %s", tab, path,
                     f"{len(found)} listed, taking the newest {min(limit, len(found))}" if found
                     else "none found" if selected else "the channel has no such tab")
            items += [(kind, item_id, title or f"{kind} {item_id}") for item_id, title in found[:limit]]
            pause(*PAGE_WAIT)
        if not items:
            raise YouTubeError("no videos or posts found on that channel")
        handle = (meta.get("vanityChannelUrl") or "").rsplit("/", 1)[-1]
        return meta["externalId"], handle if handle.startswith("@") else meta["externalId"], items

    def open(self, kind, item_id):
        """Load a video or post page. Return (owner_channel_id, continuation token of the comment section)."""
        self.comments_api = "browse" if kind == "post" else "next"
        data = self.page(f"{ORIGIN}/post/{item_id}" if kind == "post" else f"{ORIGIN}/watch?v={item_id}", kind)
        owner = find_key(find_key(data, "backstagePostRenderer" if kind == "post" else "videoOwnerRenderer"),
                         "browseId")
        if not owner:
            raise YouTubeError(f"no {kind} found at that link; it may be private, removed or mistyped")
        section = next((s for s in find_all(data, "itemSectionRenderer")
                        if s.get("sectionIdentifier") == "comment-item-section"), None)
        token = find_key(find_key(section, "continuationItemRenderer"), "token")
        if not token:
            raise YouTubeError(f"this {kind} has no comment section; comments are probably turned off")
        return owner, token

    def commenters(self, token):
        """channel_id -> {"name", "comments": [{"id", "text"}]} for the newest top-level comments.
        Replies are skipped."""
        first = self.post(self.comments_api, continuation=token)
        newest = [i for i in dig(find_key(first, "sortFilterSubMenuRenderer"), "subMenuItems", default=[])
                  if "new" in (i.get("title") or "").lower()]
        token = find_key(newest, "token")
        data = self.post(self.comments_api, continuation=token) if token else first

        authors, ids, seen = {}, set(), 0
        while seen < MAX_COMMENTS:
            token, before = None, seen
            for c in find_all(data, "commentEntityPayload"):
                props, author = c.get("properties", {}), c.get("author", {})
                if props.get("replyLevel") or not author.get("channelId") \
                        or props.get("commentId") in ids | {None}:
                    continue
                ids.add(props["commentId"])
                seen += 1
                a = authors.setdefault(author["channelId"], {"name": author.get("displayName"), "comments": []})
                a["comments"].append({"id": props["commentId"], "text": dig(props, "content", "content") or ""})
            # the next page is the continuation item at the top level of the list (nested ones are replies)
            for items in find_all(data, "continuationItems"):
                for item in items:
                    token = find_key(item.get("continuationItemRenderer"), "token") or token
            log.debug("comment page read: %d top-level comments so far, %d commenters", seen, len(authors))
            if not token or seen == before:  # last page, or a page with nothing new
                break
            pause(*PAGE_WAIT)
            try:
                data = self.post(self.comments_api, continuation=token)
            except YouTubeError as e:
                log.warning("stopped reading comments after %d: %s", seen, e)
                break

        if not authors:
            stated = "".join(r.get("text", "") for r in dig(find_key(first, "countText"), "runs", default=[]))
            if re.search(r"[1-9]", stated):
                raise YouTubeError(f'YouTube reports "{stated.strip()}" but none could be read; '
                                   "the comment format has probably changed")
            raise YouTubeError("there are no comments yet")
        return authors

    def profile_card(self, channel_id, owner, comment_id):
        """(card, None) for the commenter's card as shown on `owner`'s channel, or (None, why it failed)."""
        try:
            data = self.post("account/get_profile_card", profileOwnerExternalChannelId=channel_id,
                             profileCardContext=card_context(owner, comment_id))
        except YouTubeError as e:
            return None, str(e)
        card = find_key(data, "profileCardViewModel")
        ident = dig(card, "profileIdentityInfo", "profileIdentityInfoViewModel", default={})
        if not ident.get("channelDisplayName"):
            return None, "reply had no profile card" if card is None else "profile card format has changed"
        info = dig(card, "profileInfo", "profileInfoViewModel", default={})
        comments = []
        for c in info.get("comments", []):
            v = c.get("commentInteractionViewModel", {})
            cmd = find_key(v, "getCommentsFromInboxCommand") or {}
            comments.append({"id": cmd.get("linkedCommentId"), "video_id": cmd.get("videoId"),
                             "where": v.get("videoTitle"), "text": v.get("commentContent", "")})
        subs = re.search(r"([\d.,]+)\s*([KMB])?", ident.get("rightOfBulletInfo") or "")
        return {
            "name": ident.get("channelDisplayName"),
            "handle": ident.get("channelHandle"),
            "joined": ident.get("leftOfBulletInfo"),
            "subscribers": subs and int(float(subs.group(1).replace(",", ""))
                                        * {"K": 1e3, "M": 1e6, "B": 1e9}.get(subs.group(2), 1)),
            "comment_badge": dig(info, "profileBadgeInfoChannelWide", "commentBadge", "commentDesc"),
            "comments": comments,
        }, None


def read_item(kind, item_id, client_version):
    """Read one video's or post's comments on a session of its own, so several can run at once.
    Return (owner_channel_id, commenters, client_version), or the YouTubeError that stopped it."""
    yt = YouTube()
    yt.client_version = client_version
    try:
        owner, token = yt.open(kind, item_id)
        return owner, yt.commenters(token), yt.client_version
    except YouTubeError as e:
        return e


def card_context(owner_channel_id, comment_id):
    """profileCardContext token: a protobuf with field 2 = owner channel, field 3 = comment ID.
    The owner channel decides which comments the card lists; the comment ID is not validated."""
    def varint(n):
        out = bytearray()
        while True:
            b, n = n & 0x7F, n >> 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def field(number, payload):
        return varint((number << 3) | 2) + varint(len(payload)) + payload

    inner = field(2, owner_channel_id.encode()) + field(3, comment_id.encode())
    raw = bytes.fromhex("82f5dcc306") + varint(len(inner)) + inner
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def joined_years(text):
    m = re.search(r"(\d+)\s+(year|month|week|day)", text or "")
    if not m:
        return None
    return int(m.group(1)) * {"year": 1, "month": 1 / 12, "week": 1 / 52, "day": 1 / 365}[m.group(2)]


def all_comments(author, card):
    """The author's comments on the scanned videos/posts plus the ones listed on their card,
    without duplicates."""
    pool = {c["id"]: c for c in author["comments"]}
    for c in (card or {}).get("comments", []):
        pool.setdefault(c["id"] or f"card-{len(pool)}", c)
    return list(pool.values())


embedder = None  # the embedding model, loaded by load_embedder() when --meaning is given


def load_embedder():
    """Load the embedding model for --meaning (downloaded on first use).
    Return None, or why it could not be loaded (it runs in a thread, so it cannot end the run itself)."""
    global embedder
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        return f"--meaning needs extra packages ({e}); see requirements-meaning.txt"
    log.info("loading the meaning model %s (downloaded on first use, about 1.5 GB)", MEANING_MODEL)
    started = time.monotonic()
    try:
        import torch
        # full precision on a CPU: the half precision the model is stored in is about 5x slower there
        # and less exact. Text only: the model's image and audio encoders are left out.
        on_gpu = torch.cuda.is_available() and torch.cuda.is_bf16_supported()
        embedder = SentenceTransformer(MEANING_MODEL, config_kwargs={"vision_config": None, "audio_config": None},
                                       model_kwargs={"torch_dtype": torch.bfloat16 if on_gpu else torch.float32})
    except Exception as e:  # missing package, failed download, unsupported model
        return f"could not load {MEANING_MODEL}: {type(e).__name__}: {str(e).strip().splitlines()[0]}"
    log.info("meaning model ready after %.0f s", time.monotonic() - started)


def meaning_vectors(texts):
    """One unit-length embedding per text (their dot product is the cosine similarity),
    or None for each when --meaning is off."""
    if embedder is None or not texts:
        return [None] * len(texts)
    return embedder.encode(texts, prompt_name="SentenceSimilarity", normalize_embeddings=True,
                           show_progress_bar=False).astype("float32")


def score(comments, card, meaning=True):
    """(points, reasons, text similarity of the closest pair of comments, their closest
    similarity in meaning or None without --meaning, account age in years).
    `meaning=False` skips the meaning check even when the model is loaded.
    Comments that are part of a flagged pair get "matched": True, so a reviewer can see which ones counted."""
    cross = same = reworded = 0
    best, best_meaning = 0.0, None
    texts = [re.sub(r"\s+", " ", c["text"].lower()).strip() for c in comments]
    long_enough = [i for i, t in enumerate(texts) if len(t) >= MIN_TEXT_LEN]
    vectors = dict(zip(long_enough, meaning_vectors([comments[i]["text"] for i in long_enough]) if meaning
                       else [None] * len(long_enough)))
    for i, j in itertools.combinations(long_enough, 2):
        a, b = comments[i], comments[j]
        matcher = difflib.SequenceMatcher(None, texts[i], texts[j], autojunk=False)  # autojunk skews long texts
        # quick_ratio() is a cheap upper bound: the slow exact ratio is only worked out when it could matter
        ratio = matcher.ratio() if matcher.quick_ratio() >= min(best, SIMILARITY) else 0.0
        best = max(best, ratio)
        # min(): rounding can put identical texts a hair above 1
        alike = None if vectors[i] is None else min(1.0, float(vectors[i] @ vectors[j]))
        if alike is not None:
            best_meaning = max(best_meaning or 0.0, alike)
        if ratio >= SIMILARITY:
            if a["video_id"] != b["video_id"]:
                cross += 1
            else:
                same += 1
            a["matched"] = b["matched"] = True
        elif alike is not None and alike >= MEANING_SIMILARITY:  # only pairs the text check did not catch
            reworded += 1
            a["matched"] = b["matched"] = True

    pts, reasons = 0, []
    if cross:
        pts += W_CROSS_VIDEO_COPY
        reasons.append(f"near-identical comments across videos ({cross})")
    if same:
        pts += W_REPEAT_ON_VIDEO
        reasons.append(f"near-identical comments on the same video or post ({same})")
    if reworded:
        pts += W_SAME_MEANING
        reasons.append(f"reworded comments with the same meaning ({reworded})")
    years = joined_years((card or {}).get("joined"))
    if years is not None and years < NEW_ACCOUNT_YEARS:
        pts += W_NEW_ACCOUNT
        reasons.append("very new account")
    return pts, reasons, round(best, 3), best_meaning and round(best_meaning, 3), years


def cell(text):
    """Free text for a CSV cell: one line, and never starting with a character that makes
    a spreadsheet run it as a formula (comments are written by strangers)."""
    text = " ".join((text or "").split())
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def comment_link(c):
    if not c.get("id") or not c.get("video_id"):
        return None
    if c.get("kind") == "post":  # card comments have no kind: the card lists video comments only
        return f"{ORIGIN}/post/{c['video_id']}?lc={c['id']}"
    return f"{ORIGIN}/watch?v={c['video_id']}&lc={c['id']}"


def parse_target(link):
    """("post" | "video" | "channel", post ID / video ID / channel path) for a link, or None."""
    post = re.search(r"(?:/post/|[?&]lb=)(Ug[\w-]+)", link)
    video = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})", link) \
        or re.fullmatch(r"([\w-]{11})", link)
    channel = re.search(r"youtube\.com/(@[^/?#\s]+|channel/UC[\w-]+|c/[^/?#\s]+|user/[^/?#\s]+)", link) \
        or re.fullmatch(r"(@[^/?#\s]+)", link)
    for kind, found in (("post", post), ("video", video), ("channel", channel)):
        if found:
            return kind, found.group(1)
    return None


def merge_commenters(authors, found, kind, item_id, label):
    """Add one video's or post's commenters (from YouTube.commenters) to `authors`:
    channel_id -> {"name", "comments"}, each comment tagged with where it was left."""
    for channel_id, a in found.items():
        authors.setdefault(channel_id, {"name": a["name"], "comments": []})["comments"] += [
            {**c, "kind": kind, "video_id": item_id, "where": label} for c in a["comments"]]


def pick_commenters(authors, owner):
    """The commenters to check, as [(channel_id, author)]: not the channel itself, at most
    MAX_AUTHORS, those seen on the most videos/posts first so they survive the cut."""
    others = [(channel_id, a) for channel_id, a in authors.items() if channel_id != owner]
    return sorted(others, key=lambda a: -len({c["video_id"] for c in a[1]["comments"]}))[:MAX_AUTHORS]


def account_row(channel_id, author, card, why, scored):
    """The account's CSV columns. `card` and `why` come from profile_card(), `scored` from score()."""
    pts, reasons, best, best_meaning, years = scored
    card = card or {}
    return {
        "score": pts,
        "reasons": "; ".join(reasons),
        "handle": card.get("handle"),
        "name": cell(card.get("name") or author["name"]),
        "channel_id": channel_id,
        "joined": card.get("joined"),
        "joined_years": None if years is None else round(years, 2),
        "subscribers": card.get("subscribers"),
        "comment_badge": card.get("comment_badge"),
        "max_similarity": best,
        "max_meaning_similarity": best_meaning,
        "card": "ok" if card else f"failed: {why}",
    }


CSV_COLUMNS = ["score", "reasons", "handle", "name", "channel_id", "joined", "joined_years", "subscribers",
               "comment_badge", "max_similarity", "max_meaning_similarity", "card",
               "comment_on", "comment_link", "comment_text"]


def write_csv(f, accounts):
    """Write [(account_row, comments)] to an open text file: one row per comment, the account's
    columns repeated on each of its rows, highest-scoring accounts first. Returns the row count."""
    accounts = sorted(accounts, key=lambda a: (-a[0]["score"], -a[0]["max_similarity"]))
    rows = [{**account, "comment_on": cell(c["where"]), "comment_link": comment_link(c),
             "comment_text": cell(c["text"])}
            for account, comments in accounts for c in comments]
    if rows:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def setup_logging(verbose=False):
    """Log to stderr. Level: DEBUG with `verbose`, else $BOTORNOT_LOG, else INFO."""
    level = "DEBUG" if verbose else os.environ.get("BOTORNOT_LOG", "INFO").upper()
    logging.basicConfig(format="%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s", datefmt="%H:%M:%S")
    try:
        log.setLevel(level)
    except ValueError:
        log.setLevel("INFO")
        log.warning("BOTORNOT_LOG=%s is not a log level; using INFO", level)


def stop(message):
    log.error(message)
    sys.exit(1)


def main():
    flags = {"--meaning", "--verbose"}
    args = [a for a in sys.argv[1:] if a not in flags]
    if len(args) != 1 or args[0].startswith("--"):
        sys.exit(__doc__)
    setup_logging("--verbose" in sys.argv)
    threading.current_thread().name = "main"
    started = time.monotonic()
    target = parse_target(args[0])
    if not target:
        stop("could not find a channel, video or post in that link")

    # one background thread: it loads the --meaning model while the comments are read,
    # then scores each account while the profile cards are still coming in
    background = ThreadPoolExecutor(max_workers=1, thread_name_prefix="score")
    loading = background.submit(load_embedder) if "--meaning" in sys.argv else None

    yt = YouTube()
    owner = None
    if target[0] == "channel":
        try:
            owner, name, items = yt.channel(target[1])
        except YouTubeError as e:
            stop(str(e))
    else:
        kind, name = target
        items = [(kind, name, f"this {kind}")]

    authors = {}  # channel_id -> {"name", "comments"}, merged over every video and post read
    log.info("reading comments of %d videos/posts, %d at a time", len(items), min(WORKERS, len(items)))
    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="comments") as pool:
        results = pool.map(lambda item: read_item(item[0], item[1], yt.client_version), items)
        for (kind, item_id, label), result in zip(items, results):  # in list order, whichever finishes first
            if isinstance(result, YouTubeError):
                if len(items) == 1:
                    stop(str(result))
                log.warning("skipped %s %s: %s", kind, item_id, result)
                continue
            item_owner, found, yt.client_version = result
            owner = owner or item_owner
            merge_commenters(authors, found, kind, item_id, label)
            log.info("%s %s: %d comments from %d commenters", kind, item_id,
                     sum(len(a["comments"]) for a in found.values()), len(found))
    if not authors:
        stop(f"no comments could be read from any of the {len(items)} videos and posts")
    todo = pick_commenters(authors, owner)
    if not todo:
        stop("the only top-level comments are from the channel owner; nothing to check")
    if loading and loading.result():  # before the profile cards, so a missing model does not waste them
        stop(loading.result())
    log.info("%s, owner %s: %d commenters, checking %d, %d profile cards at a time", name, owner,
             len(authors) - (owner in authors), len(todo), min(CARD_WORKERS, len(todo)))

    lock, local, gave_up = threading.Lock(), threading.local(), threading.Event()
    failures = [0]  # profile cards that failed in a row, over all card workers

    def fetch_card(channel_id, author):
        """(card, why) from a session of this thread's own, or None once the run has given up on cards."""
        if gave_up.is_set():
            return None
        if hasattr(local, "yt"):
            pause(*CARD_WAIT)
        else:
            local.yt = YouTube()
            local.yt.client_version = yt.client_version
            pause(0, CARD_WAIT[0])  # so the workers do not all start in the same instant
        if gave_up.is_set():
            return None
        card, why = local.yt.profile_card(channel_id, owner, author["comments"][0]["id"])
        with lock:
            failures[0] = 0 if card else failures[0] + 1
            if failures[0] == 3:
                gave_up.set()
                log.warning("3 profile cards in a row failed, so the rest are skipped. "
                            "YouTube is probably blocking these requests or has changed the endpoint.")
        return card, why

    checked = []
    with ThreadPoolExecutor(max_workers=CARD_WORKERS, thread_name_prefix="card") as pool:
        cards = [pool.submit(fetch_card, channel_id, author) for channel_id, author in todo]
        for i, ((channel_id, author), result) in enumerate(zip(todo, cards), 1):
            if result.result() is None:
                continue
            card, why = result.result()
            if card:
                log.info("[%d/%d] %s: %s, %s, %d comments on the card", i, len(todo), card["name"],
                         card["joined"] or "join date unknown", card["comment_badge"] or "no comment badge",
                         len(card["comments"]))
            else:
                log.warning("[%d/%d] %s: no profile card (%s)", i, len(todo), author["name"], why)
            comments = all_comments(author, card)
            checked.append((channel_id, author, card, why, comments, background.submit(score, comments, card)))

    accounts = [(account_row(channel_id, author, card, why, scoring.result()), comments)
                for channel_id, author, card, why, comments, scoring in checked]
    for account, _ in accounts:
        if account["score"]:
            log.info("flagged: %s (%s) scored %d: %s", account["name"], account["handle"] or account["channel_id"],
                     account["score"], account["reasons"])
    out = f"{name}.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        rows = write_csv(f, accounts)
    failed = sum(a["card"] != "ok" for a, _ in accounts)
    log.info("wrote %s: %d accounts, %d comments, %d accounts with score > 0%s, in %.0f s", out, len(accounts),
             rows, sum(a["score"] > 0 for a, _ in accounts),
             f", {failed} without a profile card" if failed else "", time.monotonic() - started)
    if len(accounts) < len(todo):
        log.warning("%d of %d commenters were not checked", len(todo) - len(accounts), len(todo))


if __name__ == "__main__":
    main()
