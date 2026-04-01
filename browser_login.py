"""
browser_login.py — Managed browser login session (v2)

Architecture ₆ no live Playwright objects cross thread boundaries:

  Phase 1 —""launch login browser"
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

  Phase 4 —""cleanup"
    finish_session(session_id, delete_state_file=True)
      Removes the session from memory and deletes the temp state file.

Security:
  - No credentials are ever stored to disk; only post-login cookies /
    localStorage (Playwright storage state format).
  
  Temp state files are deleted after the scrape unless requested otherwise.
  - Session IDs are random hex strings.

Requires:
  pip install playwright
  playwright install chromium
"""