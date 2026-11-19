import os

import pytest

from scripts.client import make_bundle
from scripts.setup import initialize
from supernova.contracts import validate_bundle


def test_setup_creates_private_independent_configuration_and_preserves_it(tmp_path):
    initialize(tmp_path, "http://127.0.0.1:18081", 8111)
    config = tmp_path / ".env"
    token = tmp_path / ".orbitowl/api-token"
    original = token.read_text()
    assert len(original.strip()) >= 32
    assert config.stat().st_mode & 0o777 == 0o600
    assert token.stat().st_mode & 0o777 == 0o600
    assert config.read_text().startswith(f"ORBITOWL_UID={os.getuid()}\n")
    assert "ORBITOWL_PORT=8111\n" in config.read_text()
    assert original.strip() not in config.read_text()
    initialize(tmp_path, "http://127.0.0.1:9999", 8112)
    assert original == token.read_text()
    assert "ORBITOWL_PORT=8111\n" in config.read_text()


def test_unsafe_runner_url_is_rejected_before_writing(tmp_path):
    with pytest.raises(ValueError, match="without credentials"):
        initialize(tmp_path, "http://user:password@localhost:18081", 8110)
    assert not list(tmp_path.iterdir())


def test_standalone_client_creates_api_bundle(tmp_path):
    source = tmp_path / "manual.txt"
    source.write_text("Example document", encoding="utf-8")
    key, body = make_bundle([source])
    manifest = validate_bundle(body)
    assert manifest["build_id"] == key
    assert [item["filename"] for item in manifest["documents"]] == ["manual.txt"]
