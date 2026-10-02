"""The record's endpoints (#137): the view, and Undo with its confirmation.

Known-good: GET shows the session's record with the running programs beside it;
POST undo restores the backed-up config byte for byte and keeps created files.

Known-bad: removing created files without `confirm: true` is refused and
removes nothing; a session that changed nothing outside has an empty record.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_ui3_mode import NoModel

from saddle.sessions import SessionStore
from saddle.sideeffects import SideEffects
from saddle.web.app import build_app


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    place = tmp_path / "home"
    place.mkdir()
    monkeypatch.setenv("HOME", str(place))
    return place


def test_the_record_endpoint_shows_the_session_record_and_undo_needs_confirmation_to_remove(
    tmp_path: Path, home: Path
) -> None:
    store = SessionStore(tmp_path / "s")
    app = build_app(store, NoModel, default_workdir=tmp_path)
    sid = store.create(title="t", workdir=str(tmp_path)).id
    config = home / "c.conf"
    made = home / "made.txt"
    config.write_bytes(b"a=1\r\n")
    with TestClient(app) as client:
        empty = client.get(f"/api/sessions/{sid}/outside").json()
        assert (empty["empty"], empty["files"], empty["processes"]) == (True, [], [])
        record = SideEffects(store.outside_dir(sid))
        record.before_file(config, via="edit_file")
        record.before_file(made, via="write_file")
        config.write_bytes(b"a=2\n")
        made.write_text("new")
        view = client.get(f"/api/sessions/{sid}/outside").json()
        assert {Path(f["path"]).name: f["change"] for f in view["files"]} == {
            "c.conf": "changed",
            "made.txt": "created",
        }
        assert (view["can_restore"], view["can_delete"]) == (1, 1)
        refused = client.post(f"/api/sessions/{sid}/outside/undo", json={"delete_created": True})
        assert refused.status_code == 400
        assert made.exists()
        assert config.read_bytes() == b"a=2\n"
        restored = client.post(f"/api/sessions/{sid}/outside/undo", json={}).json()
        assert restored["restored"] == [str(config)]
        assert restored["kept"] == [str(made)]
        assert config.read_bytes() == b"a=1\r\n"
        assert made.exists()
        removed = client.post(
            f"/api/sessions/{sid}/outside/undo", json={"delete_created": True, "confirm": True}
        ).json()
        assert removed["deleted"] == [str(made)]
        assert removed["record"]["files"] == []
        assert not made.exists()
