"""Fetch IT4WIFI NHK credentials from the MyNice cloud account."""
from __future__ import annotations

import base64
import logging
import re

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://integration.niceappdomain.com/myNiceCloud/"
CLIENT_ID = "android-client-id"
CLIENT_SECRET = "android-client-id_21"
HEADERS = {
    "OS": "Android",
    "OSVersion": "13",
    "DeviceModel": "Google Pixel pixel",
    "Accept-Language": "en",
    "Accept": "application/json",
}
TIMEOUT = aiohttp.ClientTimeout(total=30)


class MyNiceCloudError(Exception):
    """Generic cloud communication error."""


class MyNiceCloudAuthError(MyNiceCloudError):
    """Invalid MyNice account credentials."""


class MyNiceCloudNotFound(MyNiceCloudError):
    """No credentials for the requested MAC address."""


def _norm_mac(value: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", value or "").upper()


async def async_fetch_credentials(
    session: aiohttp.ClientSession, email: str, password: str, mac: str
) -> tuple[str, str]:
    """Return (username, base64 password) of the IT4WIFI with given MAC."""
    try:
        async with session.post(
            f"{BASE_URL}oauth/token",
            params={"grant_type": "password", "username": email, "password": password},
            auth=aiohttp.BasicAuth(CLIENT_ID, CLIENT_SECRET),
            headers=HEADERS,
            timeout=TIMEOUT,
        ) as resp:
            if resp.status in (400, 401):
                raise MyNiceCloudAuthError("MyNice login rejected")
            if resp.status != 200:
                raise MyNiceCloudError(f"Token request failed: HTTP {resp.status}")
            token = (await resp.json(content_type=None)).get("access_token")
        if not token:
            raise MyNiceCloudError("Token missing in response")

        async with session.get(
            f"{BASE_URL}api/v1/macrouser/user",
            headers={**HEADERS, "Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        ) as resp:
            if resp.status != 200:
                raise MyNiceCloudError(f"Credential request failed: HTTP {resp.status}")
            payload = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise MyNiceCloudError(str(err)) from err
    except TimeoutError as err:
        raise MyNiceCloudError("Cloud request timed out") from err

    wanted = _norm_mac(mac)
    data = payload.get("data") or {}
    for device in data.get("smartDevices") or []:
        for rec in device.get("accessoryCredentials") or []:
            if _norm_mac(rec.get("accessoryMacAddress", "")) != wanted:
                continue
            username = rec.get("accessoryUser")
            hex_pwd = rec.get("accessoryPassword", "")
            try:
                b64_pwd = base64.b64encode(bytes.fromhex(hex_pwd)).decode()
            except ValueError as err:
                raise MyNiceCloudError("Unexpected password format") from err
            if username:
                _LOGGER.debug("Credentials for %s loaded from MyNice cloud", mac)
                return username, b64_pwd
    raise MyNiceCloudNotFound(f"No credentials for {mac}")
