"""Async API client for IKB Direkt (direkt.ikb.at)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import logging
import re
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

    counting_point: str  # e.g. AT0051000602000000000000000042791
    meter_number: str  # e.g. 1KFM0200317655
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
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
        return self._session

    async def close(self) -> None:
        """Close the session if created internally."""
        if not self._external_session and self._session and not self._session.closed:
            await self._session.close()

    async def login(self) -> bool:
        """Authenticate via direkt.ikb.at and Keycloak SSO."""
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

            soup = BeautifulSoup(html, "html.parser")
            csrf_meta = soup.find("meta", {"name": "csrf-token"})
            if not csrf_meta or not csrf_meta.get("content"):
                raise IKBDataError("CSRF token not found on IKB Direkt portal")
            self._csrf_token = csrf_meta["content"]

            # 2. Trigger Keycloak login redirect via AJAX call
            _LOGGER.debug("Requesting SSO redirect from portal")
            ajax_headers = {
                "X-CSRF-Token": self._csrf_token,
                "X-Requested-With": "XMLHttpRequest",
            }
            async with session.post(
                PORTAL_LOGIN_URL,
                data={"form[send]": ""},
                headers=ajax_headers,
            ) as resp:
                if resp.status != 200:
                    raise IKBConnectionError(
                        f"Login initiate failed (status {resp.status})"
                    )
                login_data = await resp.json(content_type=None)

            redirect_url = login_data.get("redirect")
            if not redirect_url:
                raise IKBDataError("No SSO redirect URL returned by IKB portal")

            # 3. Fetch Keycloak login form
            _LOGGER.debug("Fetching SSO auth page: %s", redirect_url)
            async with session.get(redirect_url) as resp:
                if resp.status != 200:
                    raise IKBConnectionError(
                        f"Failed to load SSO page (status {resp.status})"
                    )
                sso_html = await resp.text()

            sso_soup = BeautifulSoup(sso_html, "html.parser")
            form = sso_soup.find("form")
            if not form or not form.get("action"):
                raise IKBDataError("SSO login form not found")

            sso_action_url = form["action"]

            # Collect form fields
            post_data: dict[str, Any] = {
                "username": self._username,
                "password": self._password,
                "login": "Anmelden",
            }
            for hidden_input in form.find_all("input", {"type": "hidden"}):
                name = hidden_input.get("name")
                if name:
                    post_data[name] = hidden_input.get("value", "")

            # 4. Submit credentials to Keycloak
            _LOGGER.debug("Submitting credentials to SSO")
            async with session.post(
                sso_action_url,
                data=post_data,
                allow_redirects=True,
            ) as resp:
                final_url = str(resp.url)
                body = await resp.text()

            # Check if login was successful
            if "sso.ikb.at" in final_url:
                # Still on SSO page -> login failure
                err_soup = BeautifulSoup(body, "html.parser")
                err_msg = "Invalid username or password"
                feedback = err_soup.find("span", class_="kc-feedback-text") or err_soup.find("div", class_="alert")
                if feedback:
                    err_msg = feedback.text.strip()
                raise IKBAuthenticationError(f"IKB login failed: {err_msg}")

            _LOGGER.info("IKB Direkt login successful")
            self._is_logged_in = True
            return True

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise IKBConnectionError(f"Network error during IKB login: {err}") from err

    async def get_meters(self) -> list[MeterPoint]:
        """Discover available meters and counting points."""
        if not self._is_logged_in:
            await self.login()

        session = await self._get_session()
        try:
            # First, visit the loadprofile page to set up the server session context
            _LOGGER.debug("Visiting loadprofile page")
            async with session.get(PORTAL_LOADPROFILE_PAGE_URL) as resp:
                if resp.status != 200:
                    raise IKBConnectionError("Failed to visit loadprofile page")

            # Then fetch loadprofile content via AJAX
            _LOGGER.debug("Fetching loadprofile content via AJAX")
            headers = {
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": self._csrf_token or "",
            }
            async with session.get(PORTAL_LOADPROFILE_AJAX_URL, headers=headers) as resp:
                if resp.status != 200:
                    raise IKBConnectionError(
                        f"Failed to fetch loadprofile data (status {resp.status})"
                    )
                data = await resp.json(content_type=None)

            content = data.get("content", "")
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
        meter: MeterPoint,
        start_date: date | datetime | str,
        end_date: date | datetime | str,
        resolution: str = RESOLUTION_15MIN,
    ) -> list[Reading]:
        """Fetch readings for the given meter and date range."""
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

        session = await self._get_session()
        params = {
            "ajaxService": "getLoadProfile",
            "ajax": "y",
            "startDate": s_str,
            "endDate": e_str,
            "resolution": resolution,
            "periods": f"[{meter.raw_json}]",
        }
        url = f"{PORTAL_DATA_SERVICE_URL}?{urllib.parse.urlencode(params)}"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-Token": self._csrf_token or "",
        }

        _LOGGER.debug("Requesting load profile: %s to %s (resolution: %s)", s_str, e_str, resolution)
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise IKBConnectionError(
                        f"Failed to fetch load profile (status {resp.status})"
                    )
                raw_json = await resp.json(content_type=None)

            if not isinstance(raw_json, list) or len(raw_json) == 0:
                _LOGGER.debug("Empty load profile response from portal")
                return []

            series = raw_json[0]
            raw_data = series.get("data", [])
            readings: list[Reading] = []

            for item in raw_data:
                # item format: [end_ts, kwh, start_ts, end_ts]
                if isinstance(item, list) and len(item) >= 3:
                    end_ts = int(item[0])
                    val = float(item[1]) if item[1] is not None else 0.0
                    start_ts = int(item[2])
                    readings.append(
                        Reading(
                            start_timestamp=start_ts,
                            end_timestamp=end_ts,
                            kwh=val,
                        )
                    )

            # Sort by start_timestamp
            readings.sort(key=lambda r: r.start_timestamp)
            _LOGGER.info(
                "Retrieved %d reading(s) for meter %s", len(readings), meter.counting_point
            )
            return readings

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise IKBConnectionError(f"Network error fetching load profile: {err}") from err
