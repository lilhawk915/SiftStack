"""Tests for src/recaptcha_v3_solver.py — BUG-04 D.2 primary fix.

Covers the 2Captcha v3 wrapper: API-key gate, empty-token handling,
retry logic, and eventual failure semantics. All 2Captcha network
calls are mocked; no live traffic.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────


def _run(coro):
    """Run an async coroutine to completion (avoids pytest-asyncio dep)."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Tests ───────────────────────────────────────────────────────────────


def test_solve_success_returns_token(monkeypatch):
    import config
    from recaptcha_v3_solver import solve_recaptcha_v3

    monkeypatch.setattr(config, "CAPTCHA_API_KEY", "fake-key")
    mock_client = MagicMock()
    mock_client.recaptcha.return_value = {"code": "TOKEN_XYZ"}

    with patch(
        "recaptcha_v3_solver.TwoCaptcha", return_value=mock_client,
    ):
        token = _run(solve_recaptcha_v3(
            url="https://pro.mcohio.org",
            sitekey="6L_test_sitekey",
            action="submit",
            min_score=0.3,
        ))

    assert token == "TOKEN_XYZ"
    mock_client.recaptcha.assert_called_once_with(
        sitekey="6L_test_sitekey",
        url="https://pro.mcohio.org",
        version="v3",
        action="submit",
        score=0.3,
        enterprise=0,
    )


def test_solve_empty_token_raises_after_retries(monkeypatch):
    import config
    from recaptcha_v3_solver import RecaptchaV3SolveError, solve_recaptcha_v3

    monkeypatch.setattr(config, "CAPTCHA_API_KEY", "fake-key")
    mock_client = MagicMock()
    mock_client.recaptcha.return_value = {"code": ""}

    with patch(
        "recaptcha_v3_solver.TwoCaptcha", return_value=mock_client,
    ):
        with pytest.raises(RecaptchaV3SolveError) as exc:
            _run(solve_recaptcha_v3(
                url="https://pro.mcohio.org",
                sitekey="6L_test_sitekey",
                action="submit",
                max_retries=2,
            ))

    assert "empty token" in str(exc.value)
    # Attempted max_retries=2 times before giving up
    assert mock_client.recaptcha.call_count == 2


def test_solve_missing_api_key_raises_immediately(monkeypatch):
    import config
    from recaptcha_v3_solver import RecaptchaV3SolveError, solve_recaptcha_v3

    monkeypatch.setattr(config, "CAPTCHA_API_KEY", "")

    with pytest.raises(RecaptchaV3SolveError) as exc:
        _run(solve_recaptcha_v3(
            url="https://pro.mcohio.org",
            sitekey="6L_test",
            action="submit",
        ))
    assert "CAPTCHA_API_KEY not set" in str(exc.value)


def test_solve_retries_on_transient_exception(monkeypatch):
    """First call raises, second call succeeds — solver returns token."""
    import config
    from recaptcha_v3_solver import solve_recaptcha_v3

    monkeypatch.setattr(config, "CAPTCHA_API_KEY", "fake-key")
    mock_client = MagicMock()
    mock_client.recaptcha.side_effect = [
        Exception("network hiccup"),
        {"code": "TOKEN_ON_RETRY"},
    ]

    with patch(
        "recaptcha_v3_solver.TwoCaptcha", return_value=mock_client,
    ):
        token = _run(solve_recaptcha_v3(
            url="https://pro.mcohio.org",
            sitekey="6L_test",
            action="submit",
            max_retries=3,
        ))
    assert token == "TOKEN_ON_RETRY"
    assert mock_client.recaptcha.call_count == 2


def test_solve_exhausted_retries_raises(monkeypatch):
    """All 3 attempts raise — solver gives up with wrapped last error."""
    import config
    from recaptcha_v3_solver import RecaptchaV3SolveError, solve_recaptcha_v3

    monkeypatch.setattr(config, "CAPTCHA_API_KEY", "fake-key")
    mock_client = MagicMock()
    mock_client.recaptcha.side_effect = [
        Exception("first"),
        Exception("second"),
        Exception("third"),
    ]

    with patch(
        "recaptcha_v3_solver.TwoCaptcha", return_value=mock_client,
    ):
        with pytest.raises(RecaptchaV3SolveError) as exc:
            _run(solve_recaptcha_v3(
                url="https://pro.mcohio.org",
                sitekey="6L_test",
                action="submit",
                max_retries=3,
            ))
    # Message should reflect the final attempt's error
    assert "third" in str(exc.value)
    assert mock_client.recaptcha.call_count == 3
