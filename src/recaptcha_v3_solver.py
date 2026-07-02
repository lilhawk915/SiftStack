"""reCAPTCHA v3 token solver via 2Captcha for pro.mcohio.org (BUG-04).

pro.mcohio.org deployed reCAPTCHA v3 invisible bot-scoring on 2026-07-01.
Unlike the v2 image-challenge solver in `captcha_solver.py` (used by the
TN pipeline), v3 is score-based and invisible: the site's JS calls
`grecaptcha.execute(sitekey, {action})` on submit, gets a token, and
posts it with the form. Google's server-side validator returns a
0.0–1.0 score; low scores get blocked.

Mitigation: pay 2Captcha to solve v3 tokens for us. Their v3 API takes
sitekey + siteurl + action + min_score, runs the challenge on their
end (real browsers behind residential IPs), and returns a token that
Google will score above the min. Cost is ~$3/1000 tokens = <$1/mo at
daily-cron cadence.

Reuses the existing `CAPTCHA_API_KEY` from the TN pipeline's 2Captcha
account. Distinct from `captcha_solver.py` (v2) — do NOT merge these.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from twocaptcha import TwoCaptcha

import config


logger = logging.getLogger(__name__)


class RecaptchaV3SolveError(RuntimeError):
    """Raised when 2Captcha fails to return a usable v3 token."""


async def solve_recaptcha_v3(
    url: str,
    sitekey: str,
    action: str,
    *,
    min_score: float = 0.3,
    max_retries: int = 3,
    logger: Any = None,
) -> str:
    """Solve reCAPTCHA v3 via 2Captcha and return the token string.

    Args:
        url: Full page URL where the reCAPTCHA runs (e.g.,
            "https://pro.mcohio.org").
        sitekey: The reCAPTCHA v3 sitekey (usually starts with "6L").
        action: The action string the site's JS passes to
            grecaptcha.execute (commonly "submit", "search", or a
            site-specific string).
        min_score: Minimum Google-side score to target (0.0-1.0).
            2Captcha aims for this or higher. Default 0.3 is 2Captcha's
            recommended baseline for v3.
        max_retries: How many times to retry on 2Captcha failure.
        logger: Optional logger override (defaults to module logger).

    Returns:
        The v3 token string, to be injected into the site's
        `#g-recaptcha-response` textarea before form submit.

    Raises:
        RecaptchaV3SolveError: If CAPTCHA_API_KEY is missing, or all
            retry attempts return empty tokens / raise exceptions.
    """
    log = logger or globals()["logger"]

    if not config.CAPTCHA_API_KEY:
        raise RecaptchaV3SolveError(
            "CAPTCHA_API_KEY not set — cannot solve reCAPTCHA v3 "
            f"for {url}"
        )

    solver = TwoCaptcha(config.CAPTCHA_API_KEY)

    # BUG-04 mitigation: if PROXY_URL is set, have 2Captcha mint the
    # token FROM the same proxy the scraper submits from. This is the
    # load-bearing fix — Google's v3 scoring penalizes IP mismatch
    # between token-minter and token-submitter. With proxy pass-through,
    # both endpoints see the same residential IP → high score → accept.
    import os as _os
    proxy_url = _os.environ.get("PROXY_URL")

    def _call_2captcha() -> dict:
        kwargs = dict(
            sitekey=sitekey,
            url=url,
            version="v3",
            action=action,
            score=min_score,
            enterprise=0,
        )
        if proxy_url:
            # 2Captcha's Python SDK expects proxy as a dict with 'type'
            # and 'uri' keys (NOT a string). Strip scheme from the URI.
            _p = proxy_url
            if "://" in _p:
                _p = _p.split("://", 1)[1]
            kwargs["proxy"] = {
                "type": "HTTPS",
                "uri": _p,
            }
        return solver.recaptcha(**kwargs)

    last_error: str = ""
    for attempt in range(1, max_retries + 1):
        try:
            log.info(
                f"2Captcha v3 solve attempt {attempt}/{max_retries} — "
                f"url={url} action={action} min_score={min_score}"
            )
            result = await asyncio.get_event_loop().run_in_executor(
                None, _call_2captcha,
            )
            token = (result or {}).get("code", "")
            if not token:
                last_error = "empty token in 2Captcha response"
                log.warning(
                    f"2Captcha returned empty token "
                    f"(attempt {attempt}/{max_retries})"
                )
                continue
            log.info(
                f"2Captcha v3 token received "
                f"(attempt {attempt}/{max_retries}, len={len(token)})"
            )
            return token
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log.warning(
                f"2Captcha v3 solve attempt {attempt}/{max_retries} "
                f"raised: {last_error}"
            )
            continue

    raise RecaptchaV3SolveError(
        f"2Captcha v3 solve failed after {max_retries} attempts — "
        f"last error: {last_error or 'empty token'} — "
        f"url={url} sitekey={sitekey[:10]}... action={action}"
    )
