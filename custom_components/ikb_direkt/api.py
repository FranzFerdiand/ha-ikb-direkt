"""Async API client for IKB Direkt (direkt.ikb.at)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import logging
import math
from typing import Any
import urllib.parse

import aiohttp
from bs4 import BeautifulSoup

from .const import (
    PORTAL_BASE_URL,
    PORTAL_DATA_SERVICE_URL,
    PORTAL_LOADPROFILE_AJAX_URL,
    PORTAL_LOADPROFILE_PAGE_URL,
    PORTAL_LOGIN_URL,
    RESOLUTION_15MIN,
)

_LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class IKBDirektError(Exception):
    """Base exception for IKB Direkt."""


class IKBAuthenticationError(IKBDirektError):
    """Authentication failed (invalid username/password)."""


class IKBConnectionError(IKBDirektError):
    """Connection error when contacting IKB portals."""


class IKBDataError(IKBDirektError):
    """Error parsing or retrieving load profile data."""


@dataclass
class MeterPoint:
    """Represents a Smart Meter point in the IKB portal."""

    counting_point: str  # e.g. AT0051000000000000000000000000000
    meter_number: str  # e.g. 1KFM0000000000
    reading_type: str  # "Verbrauch" (consumption) or "Einspeisung" (feed-in)
    period_start: str  # e.g. "30.09.2026 00:15"
    period_end: str  # e.g. "01.11.2026 00:00"
    raw_json: str  # Raw JSON value required by IKB getLoadProfile API
    display_name: str

    @classmethod
    def from_option(cls, option_value: str, option_text: str) -> MeterPoint:
        """Parse a MeterPoint from an HTML option element."""
        try:
            data = json.loads(option_value)
        except json.JSONDecodeError:
            data = {}

        # Parse text: e.g. "AT005100... - 1KFM... Verbrauch (IME, ...)"
        counting_point = ""
        meter_number = ""
        reading_type = "Verbrauch"

        parts = [p.strip() for p in option_text.split("-")]
        if len(parts) >= 1:
            counting_point = parts[0].strip()
        if len(parts) >= 2:
            sub = parts[1].strip().split()
            if sub:
                meter_number = sub[0].strip()
            if len(sub) > 1:
                reading_type = sub[1].strip()

        if not isinstance(data, dict):
            raise IKBDataError("Invalid meter definition")
        period_start = data.get("PeriodBDate", "")
        period_end = data.get("PeriodEDate", "")

        return cls(
            counting_point=counting_point,
            meter_number=meter_number,
            reading_type=reading_type,
            period_start=period_start,
            period_end=period_end,
            raw_json=option_value,
            display_name=option_text.strip(),
        )


@dataclass
class Reading:
    """Represents a single interval reading."""

    start_timestamp: int
    end_timestamp: int
    kwh: float

    @property
    def start_datetime(self) -> datetime:
        """Return interval start as UTC datetime."""
        return datetime.fromtimestamp(self.start_timestamp, tz=timezone.utc)

    @property
    def end_datetime(self) -> datetime:
        """Return interval end as UTC datetime."""
        return datetime.fromtimestamp(self.end_timestamp, tz=timezone.utc)


class IKBDirektClient:
    """Client for logging into direkt.ikb.at and fetching smart meter readings."""

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the client."""
        self._username = username
        self._password = password
        self._external_session = session is not None
        self._session = session
        self._csrf_token: str | None = None
        self._is_logged_in = False

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return existing or create new aiohttp session."""
        if self._session is None or self._session.closed:
            self._external_session = False
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                cookie_jar=aiohttp.CookieJar(),
                timeout=aiohttp.ClientTimeout(total=60),
            )
        return self._session

    async def close(self) -> None:
        """Close the session if created internally."""
        if not self._external_session and self._session and not self._session.closed:
            await self._session.close()

    async def login(self) -> bool:
        """Authenticate via direkt.ikb.at and Keycloak SSO."""
        self._is_logged_in = False
        session = await self._get_session()

        try:
            # 1. Fetch portal landing page to obtain initial cookies and CSRF token
            _LOGGER.debug("Fetching IKB Direkt landing page")
            async with session.get(PORTAL_BASE_URL) as resp:
                if resp.status != 200:
                    raise IKBConnectionError(
                        f"Failed to load IKB portal (status {resp.status})"
                    )
                html = await resp.text()

            # Check if session is already authenticated (e.g. from existing cookie jar)
            if "page=logout" in html or "id=\"metCodes\"" in html:
                _LOGGER.info("Already logged in to IKB Direkt via existing session")
                soup = BeautifulSoup(html, "html.parser")
                csrf_meta = soup.find("meta", {"name": "csrf-token"})
                if csrf_meta and csrf_meta.get("content"):
                    self._csrf_token = csrf_meta["content"]
                self._is_logged_in = True
                return True

            soup = BeautifulSoup(html, "html.parser")
            csrf_meta = soup.find("meta", {"name": "csrf-token"})
            if not csrf_meta or not csrf_meta.get("content"):
                raise IKBDataError("CSRF token not found on IKB Direkt portal")
            self._csrf_token = csrf_meta["content"]

            # 2. Trigger Keycloak login redirect via AJAX call with retry on temporary 500
            _LOGGER.debug("Requesting SSO redirect from portal")
            ajax_headers = {
                "X-CSRF-Token": self._csrf_token,
                "X-Requested-With": "XMLHttpRequest",
            }
            login_data: dict[str, Any] = {}
            for attempt in range(3):
                async with session.post(
                    PORTAL_LOGIN_URL,
                    data={"form[send]": ""},
                    headers=ajax_headers,
                ) as resp:
                    if resp.status == 200:
                        login_data = await resp.json(content_type=None)
                        break
                    if resp.status == 500 and attempt < 2:
                        _LOGGER.debug(
                            "Portal returned 500 on login initiate (attempt %d/3), retrying in 2s...",
                            attempt + 1,
                        )
                        await asyncio.sleep(2)
                        # Re-fetch landing page for fresh CSRF/session cookie
                        async with session.get(PORTAL_BASE_URL) as fresh_resp:
                            if fresh_resp.status == 200:
                                fresh_soup = BeautifulSoup(await fresh_resp.text(), "html.parser")
                                fresh_csrf = fresh_soup.find("meta", {"name": "csrf-token"})
                                if fresh_csrf and fresh_csrf.get("content"):
                                    self._csrf_token = fresh_csrf["content"]
                                    ajax_headers["X-CSRF-Token"] = self._csrf_token
                        continue
                    raise IKBConnectionError(
                        f"Login initiate failed (status {resp.status})"
                    )

            redirect_url = login_data.get("redirect")
            if not redirect_url:
                raise IKBDataError("No SSO redirect URL returned by IKB portal")

            # 3. Fetch Keycloak login form
            self._validate_auth_url(redirect_url)
            _LOGGER.debug("Fetching SSO auth page")
            async with session.get(redirect_url) as resp:
                if resp.status != 200:
                    raise IKBConnectionError(
                        f"Failed to load SSO page (status {resp.status})"
                    )
                self._validate_auth_url(str(resp.url))
                sso_page_url = str(resp.url)
                sso_html = await resp.text()

            sso_soup = BeautifulSoup(sso_html, "html.parser")
            form = sso_soup.find("form")
            if not form or not form.get("action"):
                raise IKBDataError("SSO login form not found")

            sso_action_url = urllib.parse.urljoin(sso_page_url, form["action"])
            self._validate_auth_url(sso_action_url)

            post_data = {
                field["name"]: field.get("value", "")
                for field in form.find_all("input", {"type": "hidden"})
                if field.get("name")
            }
            post_data.update(username=self._username, password=self._password, login="Anmelden")

            # Do not let 307/308 redirects forward credentials to another origin.
            async with session.post(sso_action_url, data=post_data, allow_redirects=False) as resp:
                status = resp.status
                final_url = str(resp.url)
                body = await resp.text()
                redirect = resp.headers.get("Location")
            for _ in range(10):
                if status not in (301, 302, 303, 307, 308):
                    break
                if not redirect or status in (307, 308):
                    raise IKBAuthenticationError("Unexpected redirect during credential submission")
                final_url = urllib.parse.urljoin(final_url, redirect)
                self._validate_auth_url(final_url)
                async with session.get(final_url, allow_redirects=False) as resp:
                    status = resp.status
                    body = await resp.text()
                    redirect = resp.headers.get("Location")
            if status != 200:
                raise IKBConnectionError(f"Login completion failed (status {status})")

            # Check if login was successful
            if urllib.parse.urlsplit(final_url).hostname != "direkt.ikb.at" or not (
                "page=logout" in body or 'id="metCodes"' in body
            ):
                # Still on SSO page -> login failure
                err_soup = BeautifulSoup(body, "html.parser")
                err_msg = "Invalid username or password"
                feedback = err_soup.find("span", class_="kc-feedback-text") or err_soup.find("div", class_="alert")
                if feedback:
                    err_msg = feedback.text.strip()
                raise IKBAuthenticationError(f"IKB login failed: {err_msg}")

            final_soup = BeautifulSoup(body, "html.parser")
            csrf = final_soup.find("meta", {"name": "csrf-token"})
            if csrf and csrf.get("content"):
                self._csrf_token = csrf["content"]
            _LOGGER.info("IKB Direkt login successful")
            self._is_logged_in = True
            return True

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise IKBConnectionError(f"Network error during IKB login: {err}") from err

    @staticmethod
    def _validate_auth_url(url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"sso.ikb.at", "direkt.ikb.at"}
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise IKBAuthenticationError("Untrusted authentication destination")

    async def _request_json(self, url: str, *, visit_profile: bool = False) -> Any:
        """Retry an expired portal session once, without hiding malformed responses."""
        for attempt in range(2):
            if not self._is_logged_in:
                await self.login()
            session = await self._get_session()
            try:
                if visit_profile:
                    async with session.get(PORTAL_LOADPROFILE_PAGE_URL) as resp:
                        if resp.status in (401, 403):
                            raise IKBAuthenticationError("Portal session expired")
                        if resp.status != 200:
                            raise IKBConnectionError(f"Loadprofile page failed (status {resp.status})")
                headers = {"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": self._csrf_token or ""}
                async with session.get(url, headers=headers) as resp:
                    if resp.status in (401, 403):
                        raise IKBAuthenticationError("Portal session expired")
                    if resp.status != 200:
                        raise IKBConnectionError(f"Portal request failed (status {resp.status})")
                    try:
                        data = await resp.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError) as err:
                        body = await resp.text()
                        if "kc-form-login" in body or "page=login" in body or 'type="password"' in body:
                            raise IKBAuthenticationError("Portal returned login page") from err
                        raise IKBDataError("Invalid JSON from IKB portal") from err
                content = data.get("content", "") if isinstance(data, dict) else ""
                if isinstance(content, str) and any(marker in content for marker in (
                    "kc-form-login", "page=login", 'type="password"', "type='password'"
                )):
                    raise IKBAuthenticationError("Portal returned login form")
                if isinstance(data, dict) and (
                    data.get("redirect") or data.get("status") in (401, 403)
                    or data.get("error") in ("unauthorized", "Unauthenticated", "invalid_csrf", "session_expired")
                ):
                    raise IKBAuthenticationError("Portal session expired")
                return data
            except IKBAuthenticationError:
                self._is_logged_in = False
                self._csrf_token = None
                session.cookie_jar.clear()
                if attempt:
                    raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise IKBConnectionError(f"Network error contacting IKB: {err}") from err
        raise IKBAuthenticationError("Portal session expired")

    async def get_meters(self) -> list[MeterPoint]:
        """Discover available meters and counting points."""
        try:
            data = await self._request_json(PORTAL_LOADPROFILE_AJAX_URL, visit_profile=True)
            if not isinstance(data, dict) or "content" not in data:
                raise IKBDataError("Invalid meter response from IKB portal")
            content = data.get("content", "")
            if not isinstance(content, str):
                raise IKBDataError("Invalid meter content from IKB portal")
            soup = BeautifulSoup(content, "html.parser")
            met_select = soup.find("select", id="metCodes")
            if not met_select:
                _LOGGER.warning("metCodes select element not found in loadprofile page")
                return []

            meters: list[MeterPoint] = []
            for opt in met_select.find_all("option"):
                val = opt.get("value")
                text = opt.text.strip()
                if val and val.startswith("{"):
                    meters.append(MeterPoint.from_option(val, text))

            _LOGGER.info("Discovered %d meter(s) for account", len(meters))
            return meters

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise IKBConnectionError(f"Network error discovering meters: {err}") from err

    async def get_load_profile(
        self,
        meter: MeterPoint | Sequence[MeterPoint],
        start_date: date | datetime | str,
        end_date: date | datetime | str,
        resolution: str = RESOLUTION_15MIN,
    ) -> list[Reading]:
        """Fetch readings for the given meter(s) and date range."""
        if not self._is_logged_in:
            await self.login()

        # Format start and end date to DD.MM.YYYY
        def _format_date(d: date | datetime | str) -> str:
            if isinstance(d, datetime):
                return d.strftime("%d.%m.%Y")
            if isinstance(d, date):
                return d.strftime("%d.%m.%Y")
            return str(d)

        s_str = _format_date(start_date)
        e_str = _format_date(end_date)

        meters: list[MeterPoint] = [meter] if isinstance(meter, MeterPoint) else list(meter)

        # Build periods parameter, expanding PeriodEDate / PeriodBDate so portal backend
        # does not truncate queries across monthly period boundaries.
        period_objs: list[dict[str, Any]] = []
        for m in meters:
            if not m.raw_json:
                continue
            try:
                p_dict = json.loads(m.raw_json)
                if isinstance(p_dict, dict):
                    # Ensure PeriodEDate extends at least through requested end_date
                    p_dict["PeriodEDate"] = f"{e_str} 23:45"
                    if s_str:
                        p_dict["PeriodBDate"] = f"{s_str} 00:00"
                    period_objs.append(p_dict)
            except Exception:
                continue

        if not period_objs:
            periods_param = f"[{','.join(m.raw_json for m in meters if m.raw_json)}]"
        else:
            periods_param = json.dumps(period_objs)

        params = {
            "ajaxService": "getLoadProfile",
            "ajax": "y",
            "startDate": s_str,
            "endDate": e_str,
            "resolution": resolution,
            "periods": periods_param,
        }
        url = f"{PORTAL_DATA_SERVICE_URL}?{urllib.parse.urlencode(params)}"
        _LOGGER.debug(
            "Requesting load profile: %s to %s (resolution: %s, periods: %s)",
            s_str,
            e_str,
            resolution,
            periods_param,
        )
        try:
            raw_json = await self._request_json(url)
            if not isinstance(raw_json, list):
                raise IKBDataError("Invalid load profile response from IKB portal")

            by_timestamp: dict[int, Reading] = {}
            for series in raw_json:
                if not isinstance(series, dict):
                    raise IKBDataError("Invalid load profile series")
                for item in series.get("data", []):
                    if not isinstance(item, list) or len(item) < 3:
                        raise IKBDataError("Invalid load profile interval")
                    if item[1] is None:
                        continue  # Missing is not measured zero.
                    try:
                        end_ts, val, start_ts = int(item[0]), float(item[1]), int(item[2])
                    except (ValueError, TypeError, OverflowError) as err:
                        raise IKBDataError("Invalid load profile value") from err
                    if not math.isfinite(val) or end_ts <= start_ts:
                        raise IKBDataError("Invalid load profile value")
                    previous = by_timestamp.get(start_ts)
                    if previous is None or (previous.kwh == 0 and val != 0):
                        by_timestamp[start_ts] = Reading(start_ts, end_ts, val)
            readings = list(by_timestamp.values())

            # Sort by start_timestamp
            readings.sort(key=lambda r: r.start_timestamp)
            cp = meters[0].counting_point if meters else "unknown"
            _LOGGER.info(
                "Retrieved %d reading(s) for meter %s", len(readings), cp
            )
            return readings

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise IKBConnectionError(f"Network error fetching load profile: {err}") from err
