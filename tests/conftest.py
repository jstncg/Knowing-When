import json
import os
from pathlib import Path
import shutil
import tempfile

# Before anything imports app: every store, log and switch a test might open by default is a throwaway one, for the
# scripts tests run too, and still after a test's monkeypatch.undo() puts back the pins below.
SCRATCH = Path(tempfile.mkdtemp(prefix="gi-tests-"))
os.environ |= {"SOCIAL_DIR": str(SCRATCH / "social"), "TIMELINES_DB": str(SCRATCH / "social" / "timelines.sqlite"),
               "DATABASE_URL": f"sqlite:///{SCRATCH / 'pilot.sqlite'}", "GI_PAUSE_FILE": str(SCRATCH / "paused.json")}

import pytest  # noqa: E402

from app import contact, pay, providers, today  # noqa: E402
from app.store import Store  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(SCRATCH, ignore_errors=True)


class ModelStub:
    """Replaces providers._post: queued JSON replies, every payload recorded."""

    def __init__(self):
        self.replies, self.calls = [], []

    def reply(self, obj, *, stop_reason="end_turn"):
        self.replies.append({
            "stop_reason": stop_reason, "model": "stub",
            "content": [{"type": "text", "text": json.dumps(obj)}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
        return self

    async def __call__(self, url, key, payload, provider, budget, workspace_id=None):
        budget.take()
        self.calls.append({"url": url, "provider": provider, "payload": payload})
        if not self.replies:
            raise AssertionError("unexpected model call")
        return self.replies.pop(0)


@pytest.fixture
def model(monkeypatch):
    stub = ModelStub()
    monkeypatch.setattr(providers, "_post", stub)
    return stub


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'lane5.sqlite'}")


@pytest.fixture
def settings():
    return {"anthropic_key": "k", "max_calls_per_run": 8, "model": "claude-opus-5"}


@pytest.fixture(autouse=True)
def no_private_profiles(monkeypatch, tmp_path):  # Live never reads the real LinkedIn profile read in a test
    monkeypatch.setattr(today, "PROFILES", tmp_path / "linkedin-profiles.json")


@pytest.fixture(autouse=True)
def no_real_ledger_or_pause(monkeypatch, tmp_path):  # never the real ledgers, and never the real pause switch
    monkeypatch.setattr(contact, "LEDGERS", {})
    monkeypatch.setattr(today, "TIMELINES", tmp_path / "timelines.sqlite")  # the timing store: none until a test makes it
    monkeypatch.setenv("TIMELINES_DB", str(tmp_path / "timelines.sqlite"))  # the same for any script a test runs
    monkeypatch.setattr(today, "DAILY_LOG", tmp_path / "daily-runs.jsonl")  # and the daily run's log
    monkeypatch.setattr(contact, "PAUSE", tmp_path / "sending-paused.json")
    monkeypatch.setattr(pay, "BANDS", tmp_path / "pay-bands.json")  # nor GI's posted ranges: none unless a test writes some


@pytest.fixture(autouse=True)
def fresh_simulation():  # the simulation's store is cached per process: what one test records never reaches the next
    yield
    today._demo_store.cache_clear()
