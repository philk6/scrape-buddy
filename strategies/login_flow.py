from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright

from auth_session import load_storage_state, save_storage_state

CATALOG_PROBE_URL = "https://www.nassaucandy.com/confections.html"
ACCOUNT_PROBE_URL = "https://www.nassaucandy.com/customer/account/"


@dataclass
class LoginResult:
    success: bool
    final_url: str
    session_file: Optional[str]
    reason: str


NASSAU_LOGIN_URL = "https://www.nassaucandy.com/customer/account/login/referer/aHR0cHM6Ly93d3cubmFzc2F1Y2FuZHkuY29tL2N1c3RvbWVyL2FjY291bnQvbG9nb3V0U3VjY2Vzcy8%2C/"


def login_nassau(email: str, password: str, session_name: str = "nassau") -> LoginResult:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context_kwargs = {}
        storage_state = load_storage_state(session_name)
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        if storage_state and _probe_authenticated_state(page):
            browser.close()
            return LoginResult(True, page.url, storage_state, "existing session reused")

        page.goto(NASSAU_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        _dismiss_cookie_banner(page)
        page.fill('input[name="login[username]"]', email)
        page.fill('input[name="login[password]"]', password)
        page.click('button.action.login.primary')
        page.wait_for_timeout(3000)

        if _probe_authenticated_state(page):
            state = context.storage_state()
            path = save_storage_state(session_name, state)
            browser.close()
            return LoginResult(True, page.url, str(path), "login successful")

        browser.close()
        return LoginResult(False, page.url, None, "login failed or login success not detected")


def _dismiss_cookie_banner(page) -> None:
    try:
        page.get_by_role('button', name='Accept Cookies').click(timeout=3000)
    except Exception:
        pass


def _probe_authenticated_state(page) -> bool:
    try:
        page.goto(