"""The scanner's own logic: parsing, scoring, CSV output, retries. Nothing here uses the network."""
import base64
import io

import pytest
import requests

import botornot
from conftest import Reply


def comment(text, video="v1", comment_id=None, kind="video"):
    return {"id": comment_id or f"id-{abs(hash((text, video)))}", "text": text, "video_id": video,
            "where": "somewhere", "kind": kind}


SPAM = "I made a fortune last month thanks to this one trading adviser, what a genius!"


# ---- links ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("link, expected", [
    ("@SomeChannel", ("channel", "@SomeChannel")),
    ("https://www.youtube.com/@SomeChannel/posts", ("channel", "@SomeChannel")),
    ("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/videos", ("channel", "channel/UCabcdefghijklmnopqrstuv")),
    ("https://www.youtube.com/watch?v=abcDEF12_-x&t=10s", ("video", "abcDEF12_-x")),
    ("https://youtu.be/abcDEF12_-x", ("video", "abcDEF12_-x")),
    ("abcDEF12_-x", ("video", "abcDEF12_-x")),
    ("https://www.youtube.com/post/UgkxAbCdEfGhIjKlMnOpQrStUvWxYz012345", ("post", "UgkxAbCdEfGhIjKlMnOpQrStUvWxYz012345")),
    ("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/community?lb=UgkxABC", ("post", "UgkxABC")),
    ("hello", None),
    ("", None),
])
def test_parse_target(link, expected):
    assert botornot.parse_target(link) == expected


def test_card_context_holds_owner_and_comment():
    raw = base64.urlsafe_b64decode(botornot.card_context("UCowner", "UgxComment") + "==")
    assert raw.startswith(bytes.fromhex("82f5dcc306"))
    assert b"\x12\x07UCowner" in raw and b"\x1a\x0aUgxComment" in raw


@pytest.mark.parametrize("text, years", [
    ("Joined 3 years ago", 3), ("Joined 6 months ago", 0.5), ("Joined 1 week ago", 1 / 52), (None, None), ("", None)])
def test_joined_years(text, years):
    assert botornot.joined_years(text) == pytest.approx(years) if years is not None else botornot.joined_years(text) is None


# ---- scoring -------------------------------------------------------------------------------------

def test_score_copy_across_videos():
    pts, reasons, best, meaning, years = botornot.score([comment(SPAM, "v1"), comment(SPAM + "!", "v2")], None)
    assert pts == botornot.W_CROSS_VIDEO_COPY and "across videos" in reasons[0]
    assert best > 0.9 and meaning is None and years is None


def test_score_repeat_on_same_video():
    pts, reasons, *_ = botornot.score([comment(SPAM, "v1", "a"), comment(SPAM, "v1", "b")], None)
    assert pts == botornot.W_REPEAT_ON_VIDEO and "same video" in reasons[0]


def test_score_ignores_short_and_different_comments():
    comments = [comment("first!", "v1"), comment("first!", "v2"),
                comment("The weather has been lovely here all week long.", "v3"),
                comment("Why is the audio so quiet on this stream today?", "v4")]
    pts, reasons, best, *_ = botornot.score(comments, None)
    assert pts == 0 and reasons == [] and best < 0.9


def test_score_new_account():
    pts, reasons, *_ , years = botornot.score([comment("Just one ordinary comment here.")], {"joined": "Joined 2 weeks ago"})
    assert pts == botornot.W_NEW_ACCOUNT and reasons == ["very new account"] and years < 0.1
    assert botornot.score([], {"joined": "Joined 9 years ago"})[0] == 0


def test_score_follows_changed_weights():
    botornot.apply_settings({"W_CROSS_VIDEO_COPY": 7, "SIMILARITY": 0.99})
    assert botornot.score([comment(SPAM, "v1"), comment(SPAM, "v2")], None)[0] == 7
    assert botornot.score([comment(SPAM, "v1"), comment(SPAM + " Honestly!!", "v2")], None)[0] == 0  # under 0.99 now


def test_score_meaning_check_with_a_fake_model(monkeypatch):
    class FakeModel:  # every text gets the same vector, so every pair "means the same"
        def encode(self, texts, **kwargs):
            class Vectors(list):
                def astype(self, _):
                    return self

            class Vec(tuple):
                def __matmul__(self, other):
                    return sum(a * b for a, b in zip(self, other))
            return Vectors(Vec((1.0, 0.0)) for _ in texts)

    monkeypatch.setattr(botornot, "embedder", FakeModel())
    comments = [comment("The weather has been lovely here all week long.", "v1"),
                comment("Why is the audio so quiet on this stream today?", "v2")]
    pts, reasons, _, meaning, _ = botornot.score(comments, None)
    assert pts == botornot.W_SAME_MEANING and "reworded" in reasons[0] and meaning == 1.0
    assert botornot.score(comments, None, meaning=False)[0] == 0  # switched off for this target


# ---- settings ------------------------------------------------------------------------------------

def test_apply_settings_ranges_and_unknown_names():
    botornot.apply_settings({"MAX_COMMENTS": 60, "CARD_WAIT_MIN": 9.0, "CARD_WAIT_MAX": 11.0, "ORIGIN": "http://evil"})
    assert botornot.MAX_COMMENTS == 60 and botornot.CARD_WAIT == (9.0, 11.0)
    assert botornot.ORIGIN == "https://www.youtube.com"


def test_wait_time_stays_in_range():
    assert all(4.0 <= botornot.wait_time(4.0, 7.0) <= 7.0 for _ in range(2000))


# ---- commenters and output -----------------------------------------------------------------------

def test_merge_and_pick_commenters():
    authors = {}
    botornot.merge_commenters(authors, {"UCa": {"name": "A", "comments": [{"id": "1", "text": "x"}]},
                                        "UCowner": {"name": "Owner", "comments": [{"id": "2", "text": "y"}]}},
                              "video", "v1", "Video one")
    botornot.merge_commenters(authors, {"UCb": {"name": "B", "comments": [{"id": "3", "text": "z"}]},
                                        "UCa": {"name": "A", "comments": [{"id": "4", "text": "w"}]}},
                              "post", "p1", "post p1")
    assert authors["UCa"]["comments"][1] == {"id": "4", "text": "w", "kind": "post", "video_id": "p1", "where": "post p1"}
    picked = botornot.pick_commenters(authors, "UCowner")
    assert [c for c, _ in picked] == ["UCa", "UCb"]  # the owner is left out; the one seen on two items goes first
    botornot.MAX_AUTHORS = 1
    assert len(botornot.pick_commenters(authors, "UCowner")) == 1


def test_all_comments_has_no_duplicates():
    author = {"name": "A", "comments": [comment("mine", "v1", "same-id")]}
    card = {"comments": [{"id": "same-id", "video_id": "v1", "where": "t", "text": "mine"},
                         {"id": "other", "video_id": "v2", "where": "t", "text": "elsewhere"}]}
    assert [c["id"] for c in botornot.all_comments(author, card)] == ["same-id", "other"]
    assert len(botornot.all_comments(author, None)) == 1


def test_comment_links():
    assert botornot.comment_link({"id": "c", "video_id": "v", "kind": "video"}) == "https://www.youtube.com/watch?v=v&lc=c"
    assert botornot.comment_link({"id": "c", "video_id": "Ugp", "kind": "post"}) == "https://www.youtube.com/post/Ugp?lc=c"
    assert botornot.comment_link({"id": None, "video_id": "v"}) is None


def test_cell_defuses_spreadsheet_formulas():
    assert botornot.cell("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    assert botornot.cell("@someone hi") == "'@someone hi"
    assert botornot.cell("two\nlines  here") == "two lines here"
    assert botornot.cell(None) == ""


def test_write_csv_orders_by_score():
    def account(name, score_):
        scored = (score_, ["why"] if score_ else [], 0.5, None, 2.0)
        return botornot.account_row("UC" + name, {"name": name}, {"name": name, "handle": "@" + name}, None, scored)
    out = io.StringIO()
    rows = botornot.write_csv(out, [(account("low", 0), [comment("=1+1")]), (account("high", 3), [comment("a"), comment("b")])])
    lines = out.getvalue().splitlines()
    assert rows == 3 and lines[0].startswith("score,reasons,handle,name,channel_id")
    assert lines[1].startswith("3,why,@high") and lines[3].startswith("0,,@low") and "'=1+1" in lines[3]
    assert botornot.write_csv(io.StringIO(), []) == 0


def test_account_row_records_a_failed_card():
    row = botornot.account_row("UCx", {"name": "=Name"}, None, "HTTP 403 (blocked)", (0, [], 0.0, None, None))
    assert row["card"] == "failed: HTTP 403 (blocked)" and row["name"] == "'=Name" and row["handle"] is None


# ---- talking to YouTube (faked) ------------------------------------------------------------------

def test_profile_card_is_parsed(card_reply, fake_replies):
    yt = botornot.YouTube()
    fake_replies(yt, [Reply(data=card_reply)])
    card, why = yt.profile_card("UCcommenter", "UCowner", "UgxComment")
    assert why is None
    assert card["name"] == "Example Person" and card["handle"] == "@exampleperson"
    assert card["joined"] == "Joined 3 weeks ago" and card["subscribers"] == 1200 and card["comment_badge"] == "100+ comments"
    assert card["comments"][0] == {"id": "UgxCardComment1", "video_id": "aaaaaaaaaaa", "where": "A made-up video",
                                   "text": "I earned a fortune last month thanks to this one adviser, message them today!"}


def test_profile_card_failures_are_reported_not_raised(fake_replies):
    yt = botornot.YouTube()
    fake_replies(yt, [Reply(data={"something": "else"}), Reply(404)])
    assert yt.profile_card("UCc", "UCo", "Ugx") == (None, "reply had no profile card")
    card, why = yt.profile_card("UCc", "UCo", "Ugx")
    assert card is None and "HTTP 404" in why


def test_rate_limit_is_retried_with_doubling_waits(fake_replies, no_sleep):
    yt = botornot.YouTube()
    fake_replies(yt, [Reply(429), Reply(429), Reply(429), Reply(data={"ok": 1})])
    assert yt.post("next", continuation="x") == {"ok": 1}
    assert yt.rate_limited == 3 and len(no_sleep) == 3
    for wait, base in zip(no_sleep, (4, 8, 16)):
        assert base <= wait <= base * 1.5


def test_rate_limit_gives_up_and_respects_retry_after(fake_replies, no_sleep):
    yt = botornot.YouTube()
    fake_replies(yt, [Reply(429)] * 10)
    with pytest.raises(botornot.YouTubeError, match="rate-limited"):
        yt.post("next", continuation="x")
    assert len(no_sleep) == botornot.RATE_LIMIT_RETRIES

    no_sleep.clear()
    fake_replies(yt, [Reply(429, headers={"Retry-After": "40"}), Reply(data={})])
    yt.post("next", continuation="x")
    assert 40 <= no_sleep[0] <= 60

    no_sleep.clear()
    fake_replies(yt, [Reply(429, headers={"Retry-After": "3600"}), Reply(data={})])
    with pytest.raises(botornot.YouTubeError, match="3600 s"):
        yt.post("next", continuation="x")
    assert no_sleep == []


def test_other_failures_get_two_retries_and_404_none(fake_replies, no_sleep):
    yt = botornot.YouTube()
    fake_replies(yt, [requests.ConnectionError(), Reply(503), Reply(data={"ok": 1})])
    assert yt.post("next", continuation="x") == {"ok": 1} and len(no_sleep) == 2
    no_sleep.clear()
    fake_replies(yt, [Reply(404), Reply(data={})])
    with pytest.raises(botornot.YouTubeError, match="404"):
        yt.post("next", continuation="x")
    assert no_sleep == []


def test_non_json_reply_is_an_error(fake_replies):
    yt = botornot.YouTube()
    fake_replies(yt, [Reply(200, text="<html>are you a robot?</html>")])
    with pytest.raises(botornot.YouTubeError, match="not JSON"):
        yt.post("next", continuation="x")


def test_score_marks_the_comments_that_matched():
    comments = [comment(SPAM, "v1"), comment("The weather has been lovely here all week long.", "v2"), comment(SPAM, "v3")]
    botornot.score(comments, None)
    assert [c.get("matched", False) for c in comments] == [True, False, True]


def test_page_without_its_data_is_fetched_again(fake_replies, no_sleep):
    good = 'x "INNERTUBE_CLIENT_VERSION":"2.test" y var ytInitialData = {"a": [1, 2]};</script>'
    yt = botornot.YouTube()
    fake_replies(yt, [Reply(text="<html>no data here</html>"), Reply(text=good)])
    assert yt.page("https://www.youtube.com/watch?v=x", "video") == {"a": [1, 2]}
    assert yt.client_version == "2.test" and len(no_sleep) == 1

    no_sleep.clear()
    fake_replies(yt, [Reply(text="<html>still nothing</html>")] * 5)
    with pytest.raises(botornot.YouTubeError, match="no ytInitialData"):
        yt.page("https://www.youtube.com/watch?v=x", "video")
    assert len(no_sleep) == botornot.RETRIES
