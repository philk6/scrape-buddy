"""
browser_login.py — Managed browser login session (v2)

Architecture — no live Playwright objects cross thread boundaries:

  Phase 1 — "launch login browser"
    start_session(login_url)
      Spawns a daemon thread that opens a visible Chromium window at the
      login page.  Returns a session_id immediately.

  Phase 2 — "save session state"
    confirm_session(session_id)
      Called when the user clicks "Continue Scrape After Login".
      Signals the browser thread to:
        a) export context.storage_state() to a temp JSON file
        b) close the browser
      Blocks until the file is ready (max 30 s), then returns the file path.
      Returns None on any failure.

  Phase 3 — caller's responsibility ("reopen authenticated context" / "begin scrape")
    The Flask route receives a plain file path string and passes it to
    playwright_catalog.run(), which creates a completely fresh
    sync_playwright() session in the Flask request thread — no Playwright
    objects are ever shared across threads.

  Phase 4 — "cleanup"
    finish_session(session_id, delete_state_file=True)
      Removes the session from memory and deletes the temp state file.

Security:
  - No credentials are ever stored to disk; only post-login cookies /
    localStorage (Playwright storage state format).
  - Sessions are in-memory only; gone when Flask restarts.
  - Temp state files are deleted after the scrape unless requested otherwise.
  - Session IDs are random hex strings.

Requires:
  pip install playwright
  playwright install chromium
"""

import logging
import os
import tempfile
import threading
import time
import uuid

logger = logging.getLogger(__name__)

# In-memory session store: session_id -> session_dict
_sessions: dict = {}
_lock = threading.Lock()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _set_error(session_id: str, message: str) -> None:
    """Mark a session as errored and unblock any waiting callers."""
    with _lock:
        if session_id in _sessions:
            _sessions[session_id]["status"] = "error"
            _sessions[session_id]["error"]  = message
    # Unblock confirm_session() if it is polling
    session = _sessions.get(session_id, {})
    evt = session.get("state_ready_event")
    if evt:
        evt.set()
    logger.error(f"[BrowserLogin:{session_id}] {message}")


# ── Background browser thread ─────────────────────────────────────────────────

def _browser_thread(session_id: str, login_url: str) -> None:
    """
    Daemon thread lifecycle:
      1. Open browser, navigate to login_url    → status "waiting"
      2. Wait on confirm_event (user clicks Continue)
      3. Save storage state to temp file         → status "saving"
      4. Close browser                           → status "ready"
      5. Set state_ready_event (unblocks confirm_session)
      6. Wait on done_event (caller signals cleanup)
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _set_error(
            session_id,
            "Playwright is not installed. "
            "Run: pip install playwright && playwright install chromium",
        )
        return

    try:
        with sync_playwright() as pw:

            # ── Phase: launch login browser ───────────────────────────────────
            logger.info(f"[BrowserLogin:{session_id}] Phase: launch login browser")
            try:
                browser = pw.chromium.launch(headless=False)
            except Exception as e:
                _set_error(session_id, f"launch login browser — failed to start Chromium: {e}")
                return

            context = browser.new_context()
            page    = context.new_page()

            try:
                page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                # Non-fatal — the page may still be usable (e.g. redirect chains)
                logger.warning(f"[BrowserLogin:{session_id}] Page load warning: {e}")

            with _lock:
                _sessions[session_id]["status"] = "waiting"

            logger.info(
                f"[BrowserLogin:{session_id}] Browser open at {login_url} — "
                f"waiting for user to complete login (max 15 min)"
            )

            # ── Wait for user to log in and click "Continue" ──────────────────
            confirmed = _sessions[session_id]["confirm_event"].wait(timeout=900)

            if not confirmed:
                _set_error(
                    session_id,
                    "Login timed out — no Continue signal received within 15 minutes.",
                )
                try:
                    browser.close()
                except Exception:
                    pass
                return

            # ── Phase: save session state ─────────────────────────────────────
            logger.info(f"[BrowserLogin:{session_id}] Phase: save session state")
            with _lock:
                _sessions[session_id]["status"] = "saving"

            state_file = None
            try:
                fd, state_file = tempfile.mkstemp(
                    suffix=".json", prefix="scrape_session_"
                )
                os.close(fd)
                context.storage_state(path=state_file)
                logger.info(
                    f"[BrowserLogin:{session_id}] Session state saved → {state_file}"
                )
            except Exception as e:
                _set_error(
                    session_id,
                    f"save session state — could not export Playwright storage state: {e}",
                )
                try:
                    browser.close()
                except Exception:
                    pass
                if state_file and os.path.isfile(state_file):
                    try:
                        os.remove(state_file)
                    except Exception:
                        pass
                return

            # Close the login browser — it is no longer needed
            try:
                browser.close()
                logger.info(f"[BrowserLogin:{session_id}] Login browser closed")
            except Exception as e:
                logger.warning(f"[BrowserLogin:{session_id}] Browser close warning: {e}")

            # Signal confirm_session() that the state file is ready
            with _lock:
                _sessions[session_id]["state_file"] = state_file
                _sessions[session_id]["status"]     = "ready"
            _sessions[session_id]["state_ready_event"].set()

            logger.info(
                f"[BrowserLogin:{session_id}] State is ready — "
                f"waiting for scrape to complete before final cleanup"
            )

            # Keep session alive until caller signals done (for cleanup)
            _sessions[session_id]["done_event"].wait(timeout=7_200)

    except Exception as e:
        _set_error(session_id, f"launch login browser — unexpected error: {e}")


# ── Public API ────────────────────────────────────────────────────────────────

def start_session(login_url: str) -> str:
    """
    Open a visible browser at login_url and return a session_id.

    Raises RuntimeError if Playwright is missing or the browser fails to start.
    """
    session_id = uuid.uuid4().hex[:10]

    with _lock:
        _sessions[session_id] = {
            "confirm_event":     threading.Event(),
            "state_ready_event": threading.Event(),
            "done_event":        threading.Event(),
            "status":            "starting",
            "state_file":        None,
            "error":             None,
        }

    thread = threading.Thread(
        target=_browser_thread,
        args=(session_id, login_url),
        daemon=True,
    )
    thread.start()

    # Wait up to 5 s for the browser to open (or fail fast on import errors)
    for _ in range(50):
        time.sleep(0.1)
        with _lock:
            status = _sessions[session_id].get("status")
        if status in ("waiting", "error"):
            break

    with _lock:
        session = _sessions.get(session_id, {})

    if session.get("status") == "error":
        raise RuntimeError(session.get("error", "Browser failed to start"))

    logger.info(f"[BrowserLogin:{session_id}] Session started for {login_url}")
    return session_id


def confirm_session(session_id: str):
    """
    Signal that the user has finished logging in.

    Fires the confirm_event so the browser thread exports the session state
    to a temp file and closes the browser.  Waits up to 30 s for the file
    to be ready.

    Returns the path to the storage-state JSON file (str), or None on failure.
    The caller is responsible for passing this path to playwright_catalog.run()
    and then calling finish_session() for cleanup.
    """
    with _lock:
        session = _sessions.get(session_id)

    if not session:
        logger.warning(f"[BrowserLogin:{session_id}] confirm called for unknown session")
        return None

    if session.get("status") == "error":
        logger.error(
            f"[BrowserLogin:{session_id}] Session already in error state: "
            f"{session.get('error')}"
        )
        return None

    logger.info(f"[BrowserLogin:{session_id}] Confirm signal sent — saving session state…")
    session["confirm_event"].set()

    # Wait for the browser thread to finish saving the state file
    ready = session["state_ready_event"].wait(timeout=30)

    with _lock:
        status     = _sessions[session_id].get("status")
        state_file = _sessions[session_id].get("state_file")
        error      = _sessions[session_id].get("error")

    if not ready or status == "error":
        logger.error(
            f"[BrowserLogin:{session_id}] State save failed: {error}"
        )
        return None

    if not state_file or not os.path.isfile(state_file):
        logger.error(
            f"[BrowserLogin:{session_id}] State file missing after save: {state_file!r}"
        )
        return None

    logger.info(f"[BrowserLogin:{session_id}] State file ready: {state_file}")
    return state_file


def finish_session(session_id: str, delete_state_file: bool = True) -> None:
    """
    Clean up a session — signal the browser thread to exit and optionally
    delete the temporary state file.

    Always call this after playwright_catalog.run() completes (or fails).
    """
    with _lock:
        session = _sessions.get(session_id)

    if session:
        done_evt = session.get("done_event")
        if done_evt:
            done_evt.set()

        if delete_state_file:
            state_file = session.get("state_file")
            if state_file and os.path.isfile(state_file):
                try:
                    os.remove(state_file)
                    logger.info(
                        f"[BrowserLogin:{session_id}] Temp state file deleted: {state_file}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[BrowserLogin:{session_id}] Could not delete state file: {e}"
                    )

    with _lock:
        _sessions.pop(session_id, None)

    logger.info(f"[BrowserLogin:{session_id}] Session cleaned up")


def get_session_status(session_id: str) -> dict:
    """Return {"status": ..., "error": ...} for a session."""
    with _lock:
        session = _sessions.get(session_id)
    if not session:
        return {"status": "not_found", "error": None}
    return {
        "status": session.get("status", "unknown"),
        "error":  session.get("error"),
    }
