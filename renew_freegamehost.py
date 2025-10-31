#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto extend FreeGameHost time on https://panel.freegamehost.xyz
- Supports COOKIE or EMAIL login
- Opens target server and clicks "ADD 8 HOURS" if available
- Designed for GitHub Actions (headless). Saves screenshots for debugging.

Env vars:
  FG_LOGIN_METHOD    : "COOKIE" or "EMAIL" (default: COOKIE)
  FG_COOKIE          : Cookie header string, e.g. "cf_clearance=...; session=..." (COOKIE mode)
  FG_COOKIE_DOMAIN   : Cookie domain (default: panel.freegamehost.xyz)
  FG_EMAIL           : Email for login (EMAIL mode)
  FG_PASSWORD        : Password for login (EMAIL mode)
  FG_SERVER_KEYWORD  : A keyword to match your server card/link (e.g. "test"), optional
  FG_BASE_URL        : Base URL (default: https://panel.freegamehost.xyz)
  HEADLESS           : "true"/"false" (default: true)
  TIMEOUT_MS         : Default timeout (ms, default: 30000)

Outputs:
  - screenshots/*.png for debugging

Note:
  - COOKIE 登录更稳，能绕过 Cloudflare 挑战；EMAIL 登录可能受挑战/验证码影响。
  - FG_COOKIE 建议包含 cf_clearance、session 等关键字段。
"""

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

def log(msg: str):
    now = datetime.now().isoformat(timespec="seconds")
    print(f"[{now}] {msg}", flush=True)

def ensure_dir(p: str):
    Path(p).mkdir(parents=True, exist_ok=True)

def parse_cookie_string(cookie_string: str, domain: str):
    cookies = []
    for part in cookie_string.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        })
    return cookies

def first_visible_locator(page, selectors):
    # Try multiple selectors, return first visible locator or None
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if loc.first.is_visible(timeout=1000):
                return loc.first
        except Exception:
            continue
    return None

def wait_text(page, pattern: str, timeout=5000):
    try:
        page.locator(f'xpath=//*[contains(translate(normalize-space(.), "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "{pattern.upper()}")]').first.wait_for(state="visible", timeout=timeout)
        return True
    except PlaywrightTimeoutError:
        return False

def login_with_cookie(context, page, base_url: str, cookie_string: str, cookie_domain: str):
    if not cookie_string.strip():
        raise RuntimeError("FG_COOKIE is empty but FG_LOGIN_METHOD=COOKIE")
    # Ensure domain
    domain = cookie_domain.strip() if cookie_domain.strip() else urlparse(base_url).hostname or "panel.freegamehost.xyz"
    if not domain.startswith("."):
        domain = domain
    cookies = parse_cookie_string(cookie_string, domain)
    if not cookies:
        raise RuntimeError("Parsed cookie list is empty. Please check FG_COOKIE format.")
    context.add_cookies(cookies)
    page.goto(base_url, wait_until="domcontentloaded")
    log("Cookie applied and navigated to base URL.")

def login_with_email(page, base_url: str, email: str, password: str, default_timeout: int):
    if not email or not password:
        raise RuntimeError("FG_EMAIL or FG_PASSWORD is empty but FG_LOGIN_METHOD=EMAIL")
    login_url = base_url.rstrip("/") + "/auth/login"
    log(f"Navigating to login page: {login_url}")
    page.goto(login_url, wait_until="domcontentloaded")

    # Fill email
    email_loc = first_visible_locator(page, [
        'input[type="email"]',
        'input[name="email"]',
        'input[autocomplete="email"]',
        'input[placeholder*="mail" i]',
        'input[placeholder*="邮箱"]',
    ])
    if not email_loc:
        raise RuntimeError("Email input not found on login page.")
    email_loc.fill(email)

    # Fill password
    pwd_loc = first_visible_locator(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[autocomplete="current-password"]',
        'input[placeholder*="password" i]',
        'input[placeholder*="密码"]',
    ])
    if not pwd_loc:
        raise RuntimeError("Password input not found on login page.")
    pwd_loc.fill(password)

    # Click submit
    submit = first_visible_locator(page, [
        'button[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        'button:has-text("登录")',
        'button:has-text("登入")',
        'button:has-text("Log in")',
    ])
    if submit:
        submit.click()
    else:
        # fallback: press Enter
        pwd_loc.press("Enter")

    # Wait for navigation
    try:
        page.wait_for_load_state("networkidle", timeout=default_timeout)
    except PlaywrightTimeoutError:
        pass

    # Heuristic: if still at /auth/login, login failed
    if "/auth/login" in page.url:
        raise RuntimeError("Email login might have failed or blocked by challenge. Check credentials or try COOKIE method.")
    log("Email login done.")

def go_to_server_detail(page, server_keyword: str, default_timeout: int, screenshots_dir: str):
    # If already on server detail (Time Remaining present), return
    if wait_text(page, "Time Remaining", timeout=2000):
        log("Already on server detail page.")
        return

    # Try to click a server card by keyword
    if server_keyword:
        log(f"Trying to open server by keyword: {server_keyword}")
        candidates = [
            page.get_by_role("link", name=re.compile(server_keyword, re.I)),
            page.get_by_role("button", name=re.compile(server_keyword, re.I)),
            page.locator(f"a:has-text('{server_keyword}')"),
            page.locator(f"text={server_keyword}"),
            page.locator(f'xpath=//a[contains(translate(normalize-space(.), "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "{server_keyword.upper()}")]'),
        ]
        for loc in candidates:
            try:
                if loc.first.is_visible(timeout=1000):
                    loc.first.click()
                    break
            except Exception:
                continue
    else:
        log("No FG_SERVER_KEYWORD set, clicking the first likely server link.")
        # Try generic server links
        candidates = [
            'a[href*="/server"]',
            'a[href*="/servers"]',
            'a[href*="instance"]',
            'a:has-text("server")',
            'a:has-text("sever")',
        ]
        loc = first_visible_locator(page, candidates)
        if loc:
            loc.click()
        else:
            # Try click first card-like link
            loc2 = first_visible_locator(page, ['a', 'button'])
            if loc2:
                loc2.click()

    # Wait for detail page sign
    try:
        page.wait_for_load_state("networkidle", timeout=default_timeout)
    except PlaywrightTimeoutError:
        pass

    if not wait_text(page, "Time Remaining", timeout=5000):
        page.screenshot(path=f"{screenshots_dir}/server_open_failed.png", full_page=True)
        raise RuntimeError("Failed to open server detail page (cannot find 'Time Remaining').")

    log("Opened server detail page.")

def get_time_remaining_text(page):
    # Try to fetch surrounding text near "Time Remaining"
    try:
        loc = page.locator('xpath=//*[contains(translate(., "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "TIME REMAINING")]').first
        if loc.is_visible(timeout=1000):
            txt = loc.evaluate("el => el.closest('section,div,li,dd,dt,article')?.innerText || el.innerText")
            return re.sub(r"\s+", " ", txt or "").strip()
    except Exception:
        pass
    return ""

def click_add_8_hours(page, default_timeout: int, screenshots_dir: str):
    # Find the button/link
    sel_xpath = 'xpath=//*[self::button or self::a][contains(translate(normalize-space(.), "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "ADD 8 HOURS")]'
    btn = page.locator(sel_xpath).first

    # Record time remaining before
    before = get_time_remaining_text(page)
    if before:
        log(f"Before: {before}")

    try:
        if not btn.is_visible(timeout=3000):
            log("No visible 'ADD 8 HOURS' button found. Maybe not available yet.")
            page.screenshot(path=f"{screenshots_dir}/no_add_button.png", full_page=True)
            return False
    except PlaywrightTimeoutError:
        log("No 'ADD 8 HOURS' button detected within timeout.")
        page.screenshot(path=f"{screenshots_dir}/no_add_button_timeout.png", full_page=True)
        return False

    # Check disabled state if it's a button
    try:
        enabled = btn.is_enabled()
    except Exception:
        enabled = True

    if not enabled:
        log("'ADD 8 HOURS' button is disabled. Skipping.")
        page.screenshot(path=f"{screenshots_dir}/add_button_disabled.png", full_page=True)
        return False

    log("Clicking 'ADD 8 HOURS' ...")
    btn.click()

    # Wait for some ui update
    try:
        page.wait_for_timeout(2500)
        page.wait_for_load_state("networkidle", timeout=default_timeout)
    except PlaywrightTimeoutError:
        pass

    after = get_time_remaining_text(page)
    if after:
        log(f"After:  {after}")

    page.screenshot(path=f"{screenshots_dir}/after_click.png", full_page=True)
    log("Click done, screenshot saved.")
    return True

def main():
    base_url = os.getenv("FG_BASE_URL", "https://panel.freegamehost.xyz").rstrip("/")
    login_method = os.getenv("FG_LOGIN_METHOD", "COOKIE").strip().upper()
    cookie_string = os.getenv("FG_COOKIE", "")
    cookie_domain = os.getenv("FG_COOKIE_DOMAIN", "panel.freegamehost.xyz")
    email = os.getenv("FG_EMAIL", "")
    password = os.getenv("FG_PASSWORD", "")
    server_keyword = os.getenv("FG_SERVER_KEYWORD", "").strip()
    headless = os.getenv("HEADLESS", "true").lower() != "false"
    default_timeout = int(os.getenv("TIMEOUT_MS", "30000"))

    screenshots_dir = "screenshots"
    ensure_dir(screenshots_dir)

    log(f"Starting. Method={login_method}, Headless={headless}, Base={base_url}, Keyword={server_keyword or '(first server)'}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        )
        context.set_default_timeout(default_timeout)
        page = context.new_page()

        try:
            if login_method == "COOKIE":
                login_with_cookie(context, page, base_url, cookie_string, cookie_domain)
            elif login_method == "EMAIL":
                login_with_email(page, base_url, email, password, default_timeout)
            else:
                raise RuntimeError("FG_LOGIN_METHOD must be COOKIE or EMAIL")

            # Possible redirect to dashboard
            try:
                page.wait_for_load_state("networkidle", timeout=default_timeout)
            except PlaywrightTimeoutError:
                pass

            # Go to server detail and click
            go_to_server_detail(page, server_keyword, default_timeout, screenshots_dir)
            success = click_add_8_hours(page, default_timeout, screenshots_dir)

            if success:
                log("Success: attempted to add 8 hours.")
            else:
                log("No action performed: button not available/disabled.")

            page.screenshot(path=f"{screenshots_dir}/final.png", full_page=True)
        except Exception as e:
            log(f"ERROR: {e}")
            try:
                page.screenshot(path=f"{screenshots_dir}/error.png", full_page=True)
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()

if __name__ == "__main__":
    main()
