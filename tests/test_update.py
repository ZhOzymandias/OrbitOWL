import json
import sqlite3

import pytest

from scripts import apply_update


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    checkout = tmp_path / "repo"
    config = checkout / ".supernova"
    config.mkdir(parents=True)
    token = config / "token"
    token.write_text("test")
    database = config / "jobs.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE jobs (status TEXT)")
    (config / "service.env").write_text(
        f"SUPERNOVA_DATA={json.dumps(str(config))}\n"
        f"SUPERNOVA_TOKEN_FILE={json.dumps(str(token))}\n"
    )
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(apply_update, "STATE", state)
    calls = []

    def command(*args, **kwargs):
        calls.append((args, kwargs.get("env", {})))
        if args[0] == "git":
            return {"branch": "main", "rev-parse": "a" * 40, "status": ""}[args[3]]
        if "ps" in args:
            return args[-1] + "-container"
        if "inspect" in args:
            return args[-1] + "-image"

    monkeypatch.setattr(apply_update, "command", command)
    monkeypatch.setattr(apply_update, "ready", lambda *args, **kwargs: None)
    return checkout, database, state, calls


def test_queued_work_defers_update(server):
    checkout, database, state, calls = server
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO jobs VALUES ('queued')")
    apply_update.apply(checkout)
    assert all(args[0] == "git" for args, _ in calls)
    assert not (state / "applied").exists()


def test_job_arriving_before_stop_defers_update(server, monkeypatch):
    checkout, _, state, calls = server
    checks = iter((True, True, False))
    monkeypatch.setattr(apply_update, "idle", lambda database: next(checks))
    apply_update.apply(checkout)
    assert calls[-1][0][-2:] == ("start", "api")
    assert not (state / "applied").exists()


def test_success_and_already_applied(server):
    checkout, _, state, calls = server
    apply_update.apply(checkout)
    assert (state / "applied").read_text().strip() == "a" * 40
    image = "orbitowl:" + "a" * 40
    assert calls[-1][1]["ORBITOWL_WORKER_IMAGE"] == image
    assert calls[-1][1]["ORBITOWL_API_IMAGE"] == image
    calls.clear()
    apply_update.apply(checkout)
    assert all(args[0] == "git" for args, _ in calls)


def test_unready_candidate_restores_previous_images(server, monkeypatch):
    checkout, _, state, calls = server

    def ready(token, timeout=120, **kwargs):
        if timeout:
            raise RuntimeError("not ready")

    monkeypatch.setattr(apply_update, "ready", ready)
    with pytest.raises(RuntimeError, match="not ready"):
        apply_update.apply(checkout)
    assert (state / "failed").read_text().strip() == "a" * 40
    assert calls[-1][1]["ORBITOWL_WORKER_IMAGE"] == "orbitowl-rollback:worker"
    assert calls[-1][1]["ORBITOWL_API_IMAGE"] == "orbitowl-rollback:api"
    assert not (state / "applied").exists()
