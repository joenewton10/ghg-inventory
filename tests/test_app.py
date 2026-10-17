"""Smoke test: the Streamlit app starts and renders the overview without errors."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parent.parent / "app.py"


def test_overview_renders():
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not at.exception, at.exception
    assert any("Greenhouse gas inventory" in t.value for t in at.title)
    labels = {m.label: m.value for m in at.metric}
    assert labels["Scope 2, location-based"] == "68.8 t"
    assert labels["Scope 2, market-based"] == "140.3 t"


def test_demo_mode_offers_no_saving(monkeypatch):
    monkeypatch.setenv("GHG_APP_MODE", "demo")
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not at.exception, at.exception
    sidebar_text = " ".join(m.value for m in at.sidebar.markdown)
    assert "Demo, nothing is saved" in sidebar_text


def test_local_mode_has_no_demo_badge(monkeypatch):
    monkeypatch.setenv("GHG_APP_MODE", "local")
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert "Demo, nothing is saved" not in " ".join(m.value for m in at.sidebar.markdown)
