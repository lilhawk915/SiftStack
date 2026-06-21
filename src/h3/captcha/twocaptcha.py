"""2Captcha API client for solving image-based CAPTCHAs.

Used by county portals that gate access behind a CAPTCHA:
  - Miami probate     (probate.miamicountyohio.gov)  — text CAPTCHA on search
  - Clark probate     (probate.clarkcountyohio.gov)  — text CAPTCHA on search
  - Clermont probate  (eservices.clermontclerk.org)  — text CAPTCHA on landing
  - Butler foreclosure                                — CAPTCHA on docket
  - Clark foreclosure                                 — CAPTCHA on search

2Captcha pricing (as of 2026):
  - Normal CAPTCHA (text image): $0.50 per 1000 = $0.0005 each
  - reCAPTCHA v2: ~$2-3 per 1000

For our use case (text CAPTCHAs only), expect ~$0.0005 per case-detail page
that requires a fresh CAPTCHA solve. Volume planning:
  - Miami probate    ~24 cases / week  → ~$0.012 / week
  - Clark probate    ~115 cases / week → ~$0.058 / week
  - Clermont probate ~40 cases / week  → ~$0.020 / week
  - TOTAL: under $0.10 / week for ALL probate CAPTCHAs

API docs: https://2captcha.com/2captcha-api
"""
from __future__ import annotations

import asyncio
import base64
import os
from typing import Optional

import httpx


TWOCAPTCHA_BASE_URL = "https://2captcha.com"
DEFAULT_TIMEOUT = 120        # max seconds to wait for a solve
DEFAULT_POLL_INTERVAL = 5    # seconds between status polls


class TwoCaptchaError(Exception):
    """Raised when 2Captcha can't solve or returns an error."""


def get_api_key(input_key: Optional[str] = None) -> str:
    """Resolve the 2Captcha API key from (in order):
      1. Explicit `input_key` argument (e.g. from Actor input JSON)
      2. `CAPTCHA_API_KEY` environment variable
      3. `TWOCAPTCHA_API_KEY` environment variable

    Returns empty string if no key found (caller should handle that case).
    """
    if input_key:
        return input_key.strip()
    return (
        os.environ.get("CAPTCHA_API_KEY", "").strip()
        or os.environ.get("TWOCAPTCHA_API_KEY", "").strip()
    )


async def solve_image_captcha(
    image_bytes: bytes,
    *,
    api_key: str,
    timeout: int = DEFAULT_TIMEOUT,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    case_sensitive: bool = True,
    min_length: int = 4,
    max_length: int = 8,
    logger=None,
) -> str:
    """Solve a text-image CAPTCHA via 2Captcha.

    Args:
        image_bytes: The raw PNG/JPEG/GIF bytes of the CAPTCHA image.
        api_key: 2Captcha account API key.
        timeout: Maximum seconds to wait for a solve.
        poll_interval: Seconds between polls when waiting for the worker.
        case_sensitive: Whether case matters in the answer.
        min_length / max_length: Hints to the solver about answer length.
        logger: Optional logger (e.g. Actor.log) for status updates.

    Returns:
        The solved text answer (caller submits this to the portal's form).

    Raises:
        TwoCaptchaError on submission failure, solve failure, or timeout.
    """
    if not api_key:
        raise TwoCaptchaError("No 2Captcha API key provided")
    if not image_bytes:
        raise TwoCaptchaError("Empty image bytes")

    b64 = base64.b64encode(image_bytes).decode("ascii")
    submit_data = {
        "key": api_key,
        "method": "base64",
        "body": b64,
        "json": "1",
        "regsense": "1" if case_sensitive else "0",
        "min_len": str(min_length),
        "max_len": str(max_length),
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Submit the CAPTCHA
        submit_resp = await client.post(
            f"{TWOCAPTCHA_BASE_URL}/in.php",
            data=submit_data,
        )
        submit_resp.raise_for_status()
        submit_json = submit_resp.json()
        if submit_json.get("status") != 1:
            raise TwoCaptchaError(
                f"2Captcha submit failed: {submit_json.get('request', submit_json)}"
            )
        captcha_id = submit_json["request"]
        if logger:
            logger.info(f"  2Captcha: submitted captcha id={captcha_id}")

        # 2. Poll for the answer
        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            result_resp = await client.get(
                f"{TWOCAPTCHA_BASE_URL}/res.php",
                params={
                    "key": api_key,
                    "action": "get",
                    "id": captcha_id,
                    "json": "1",
                },
            )
            result_resp.raise_for_status()
            result_json = result_resp.json()
            status = result_json.get("status")
            answer = result_json.get("request", "")

            if status == 1:
                if logger:
                    logger.info(
                        f"  2Captcha: solved in {elapsed}s → {answer[:20]}"
                    )
                return answer
            if answer == "CAPCHA_NOT_READY":
                continue
            # Any other response = error
            raise TwoCaptchaError(
                f"2Captcha solve failed (id={captcha_id}): {answer}"
            )

    raise TwoCaptchaError(
        f"2Captcha solve timed out after {timeout}s (id={captcha_id})"
    )


async def solve_recaptcha_v2(
    *,
    api_key: str,
    sitekey: str,
    pageurl: str,
    timeout: int = 180,
    poll_interval: int = 5,
    invisible: bool = False,
    logger=None,
) -> str:
    """Solve a reCAPTCHA v2 challenge via 2Captcha.

    Args:
        api_key: 2Captcha account API key.
        sitekey: The Google reCAPTCHA site key from the page's
                 data-sitekey attribute on the .g-recaptcha div.
        pageurl: The URL of the page containing the reCAPTCHA.
        timeout: Max seconds to wait (reCAPTCHA solves take 15-120s).
        invisible: Set true for invisible reCAPTCHA v2.
        logger: Optional logger for status.

    Returns:
        The g-recaptcha-response token. Inject this into the page's hidden
        `<textarea name="g-recaptcha-response">` element, then trigger the
        site's submission flow (form submit or callback function).
    """
    if not api_key:
        raise TwoCaptchaError("No 2Captcha API key")
    if not sitekey or not pageurl:
        raise TwoCaptchaError("sitekey and pageurl required")

    submit_data = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": sitekey,
        "pageurl": pageurl,
        "json": "1",
    }
    if invisible:
        submit_data["invisible"] = "1"

    async with httpx.AsyncClient(timeout=30.0) as client:
        submit_resp = await client.post(
            f"{TWOCAPTCHA_BASE_URL}/in.php", data=submit_data,
        )
        submit_resp.raise_for_status()
        sj = submit_resp.json()
        if sj.get("status") != 1:
            raise TwoCaptchaError(
                f"2Captcha reCAPTCHA submit failed: {sj.get('request', sj)}"
            )
        captcha_id = sj["request"]
        if logger:
            logger.info(
                f"  2Captcha reCAPTCHA: submitted id={captcha_id}, "
                f"polling ..."
            )

        # First poll after ~15s (reCAPTCHA solves take >15s)
        await asyncio.sleep(15)
        elapsed = 15
        while elapsed < timeout:
            r = await client.get(
                f"{TWOCAPTCHA_BASE_URL}/res.php",
                params={
                    "key": api_key,
                    "action": "get",
                    "id": captcha_id,
                    "json": "1",
                },
            )
            r.raise_for_status()
            rj = r.json()
            status = rj.get("status")
            answer = rj.get("request", "")
            if status == 1:
                if logger:
                    logger.info(
                        f"  2Captcha reCAPTCHA: solved in {elapsed}s "
                        f"(token {len(answer)} chars)"
                    )
                return answer
            if answer == "CAPCHA_NOT_READY":
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                continue
            raise TwoCaptchaError(
                f"2Captcha reCAPTCHA failed (id={captcha_id}): {answer}"
            )

    raise TwoCaptchaError(
        f"2Captcha reCAPTCHA timed out after {timeout}s (id={captcha_id})"
    )


async def report_bad_solve(captcha_id: str, *, api_key: str) -> bool:
    """Report a bad solve back to 2Captcha to get refund credit.

    Call this when the portal rejects the solved answer — the worker who
    solved it incorrectly will be penalized and your account refunded.
    """
    if not api_key or not captcha_id:
        return False
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(
                f"{TWOCAPTCHA_BASE_URL}/res.php",
                params={
                    "key": api_key,
                    "action": "reportbad",
                    "id": captcha_id,
                    "json": "1",
                },
            )
            return resp.json().get("status") == 1
        except Exception:
            return False
