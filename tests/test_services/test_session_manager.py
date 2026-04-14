import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch

from services.session_manager import SessionManager


@pytest.fixture
def tmp_session_manager(tmp_path, monkeypatch):
    manager = SessionManager()
    monkeypatch.setattr(manager, "cookies_dir", tmp_path)
    return manager, tmp_path


def write_cookies(path: Path, platform: str, expires_offset_days: float):
    expires_ts = time.time() + expires_offset_days * 86400
    cookies = [
        {"name": "li_at", "value": "test_token", "expires": expires_ts, "domain": ".linkedin.com"},
        {"name": "JSESSIONID", "value": "ajax:123", "expires": expires_ts, "domain": "www.linkedin.com"},
    ]
    (path / f"{platform}.json").write_text(json.dumps(cookies))


def test_check_expiry_valid(tmp_session_manager):
    manager, tmp_path = tmp_session_manager
    write_cookies(tmp_path, "linkedin", expires_offset_days=30)
    is_valid, days = manager.check_expiry("linkedin")
    assert is_valid is True
    assert days > 29


def test_check_expiry_expired(tmp_session_manager):
    manager, tmp_path = tmp_session_manager
    write_cookies(tmp_path, "linkedin", expires_offset_days=-1)
    with patch("services.notifier.alert") as mock_alert:
        is_valid, days = manager.check_expiry("linkedin")
    assert is_valid is False
    assert days < 0
    mock_alert.assert_called_once()


def test_check_expiry_warns_when_close(tmp_session_manager):
    manager, tmp_path = tmp_session_manager
    write_cookies(tmp_path, "linkedin", expires_offset_days=1)
    with patch("services.notifier.alert") as mock_alert:
        is_valid, days = manager.check_expiry("linkedin")
    assert is_valid is True
    mock_alert.assert_called_once()


def test_check_expiry_no_cookies(tmp_session_manager):
    manager, _ = tmp_session_manager
    is_valid, days = manager.check_expiry("linkedin")
    assert is_valid is False
    assert days == 0.0


def test_save_and_load_cookies(tmp_session_manager):
    manager, tmp_path = tmp_session_manager
    cookies = [{"name": "test", "value": "val", "domain": ".example.com"}]
    manager.save_cookies("testplatform", cookies)
    loaded = manager.load_cookies("testplatform")
    assert loaded == cookies
