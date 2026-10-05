"""Docker packaging: Mapper CLI pin + env wiring; bez pullauta runtime."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_has_no_karttapullautin_runtime():
    """Karttapullautin / pullauta must not be downloaded or installed."""
    assert "PULLAUTA_BIN" not in DOCKERFILE
    assert "KP_VERSION" not in DOCKERFILE
    assert "KP_DOWNLOAD_URL" not in DOCKERFILE
    assert "karttapullautin/releases" not in DOCKERFILE.lower()
    assert "/usr/local/bin/pullauta" not in DOCKERFILE
    assert "name pullauta" not in DOCKERFILE.lower()


def test_dockerfile_ships_mapper_cli_pin():
    assert "mfbehrens/oo-mapper" in DOCKERFILE
    assert "6dc1fd72ce2815f47646c51102a7e8e4eedb3bb2" in DOCKERFILE
    assert "PODKLADARNA_MAPPER=/opt/mapper/bin/Mapper" in DOCKERFILE
    assert "PODKLADARNA_MAPPER_EXPORT=" in DOCKERFILE
    assert "PODKLADARNA_MAPPER_CONVERT=" in DOCKERFILE
    assert "--output-format OCD12" in DOCKERFILE
    assert "--dpi {dpi}" in DOCKERFILE
    assert "QT_QPA_PLATFORM=offscreen" in DOCKERFILE
    assert "mapper-builder" in DOCKERFILE
    assert "GPL" in DOCKERFILE


def test_compose_and_nas_scripts_have_no_kp_runtime():
    for name in (
        "docker-compose.yml",
        "docker-compose.nas.yml",
        "docker-compose.dev.yml",
        "deploy-nas.sh",
        "update-nas.sh",
        "nas-lib.sh",
    ):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "PULLAUTA_BIN" not in text, name
        assert "karttapullautin/releases" not in text.lower(), name
        assert "/usr/local/bin/pullauta" not in text, name
