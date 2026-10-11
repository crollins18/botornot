"""Shared set-up. No test here talks to YouTube: every reply is faked."""
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import botornot  # noqa: E402


@pytest.fixture(autouse=True)
def untouched_constants():
    """Put botornot.py's dials back after each test."""
    before = {name: getattr(botornot, name) for name in botornot.TUNABLE}
    yield
    for name, value in before.items():
        setattr(botornot, name, value)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Never really sleep; the list holds the waits that were asked for."""
    waits = []
    monkeypatch.setattr(botornot.time, "sleep", waits.append)
    return waits


@pytest.fixture
def card_reply():
    """A made-up get_profile_card reply in the shape YouTube sends."""
    return json.loads((ROOT / "tests" / "fixtures" / "profile_card.json").read_text())


class Reply:
    """A stand-in for requests.Response."""

    def __init__(self, status=200, data=None, headers=None, text=""):
        self.status_code, self.headers, self.url = status, headers or {}, "https://www.youtube.com/x"
        self._data, self.text = data, text
        self.content = text.encode()

    def json(self):
        if self._data is None:
            raise ValueError("not JSON")
        return self._data


@pytest.fixture
def fake_replies(monkeypatch):
    """Make a YouTube object answer from a list instead of the network: fake_replies(yt, [Reply, ...])."""
    def install(yt, replies):
        replies = iter(replies)

        def request(method, url, **kwargs):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply
        monkeypatch.setattr(yt.s, "request", request)
    return install
