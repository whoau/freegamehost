# gtx_auto_extend.py
import os
import re
import sys
import json
import time
import base64
import traceback
from pathlib import Path
from typing import List, Optional

try:
    import pyotp  # 可选：TOTP 两步验证
except Exception:
    pyotp = None

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

def now():
    return time.strftime("%Y-%m-%d_%H-%M-%S")

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

DEBUG = os.getenv("GTX_DEBUG", "0") == "1"
ART_DIR = Path(".artifacts")

def dump_debug(page, tag):
    if not DEBUG:
        return
    try:
        ART_DIR.mkdir(parents=True, exist_ok=True)
        ts = now()
        png = ART_DIR / f"{ts}_{tag}.png"
        html = ART_DIR / f"{ts}_{tag}.html"
        try:
            page.screenshot(path=str(png), full_page=True)
        except Exception:
            pass
        try:
            html.write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        log(f"[DEBUG] 已保存调试输出：{png.name} / {html.name}")
    except Exception:
        pass

def load_cookies_file(path: Path) -> List[dict]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as e:
            log(f"读取 cookies 文件失败，将忽略：{e}")
    return []

def parse_cookie_header(header: str, domain: str) -> List[dict]:
    cookies = []
    for pair in header.split(";"):
        if "=" not in pair:
            continue
        k, v = pair.strip().split("=", 1)
        if not k.strip():
            continue
        cookies.append({
            "name": k.strip(),
            "value": v.strip(),
            "domain": "." + domain if not domain.startswith(".") else domain,
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "Lax",
        })
    return cookies

def seed_cookies_from_env(domain_default: str) -> List[dict]:
    """从环境变量 GTX_COOKIE_HEADER 或 GTX_COOKIES_B64 预置 cookies"""
    cookies = []
    hdr = os.getenv("GTX_COOKIE_HEADER", "").strip()
    b64 = os.getenv("GTX_COOKIES_B64", "").strip()
    domain = os.getenv("GTX_COOKIE_DOMAIN", domain_default)
    if hdr:
        try:
            part = parse_cookie_header(hdr, domain)
            cookies.extend(part)
            log(f"已从 GTX_COOKIE_HEADER 解析 {len(part)} 条 cookies")
        except Exception as e:
            log(f"解析 GTX_COOKIE_HEADER 失败：{e}")
    if b64:
        try:
            data = base64.b64decode(b64)
            part = json.loads(data.decode("utf-8", errors="ignore"))
            if isinstance(part, list):
                cookies.extend(part)
                log(f"已从 GTX_COOKIES_B64 解码 {len(part)} 条 cookies")
        except Exception as e:
            log(f"解析 GTX_COOKIES_B64 失败：{e}")
    return cookies

def save_cookies(path: Path, cookies: List[dict]):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"已保存 cookies（{len(cookies)} 条）到 {path}")
    except Exception as e:
        log(f"保存 cookies 失败：{e}")

def has_captcha(page) -> bool:
    sels = [
        'iframe[src*="hcaptcha"]',
        'iframe[src*="recaptcha"]',
        'iframe[src*="challenges.cloudflare.com"]',
        '[class*="cf-turnstile"]',
        '#cf-chl-widget',
        'input[name="cf-turnstile-response"]',
        '[data-sitekey]',
        'text=/security check|verify you are human|人机验证|验证码/i',
    ]
    for s in sels:
        try:
            if page.locator(s).first.is_visible(timeout=500):
                return True
        except Exception:
            pass
    return False

def find_and_fill(page, selectors: list, value: str) -> bool:
    for s in selectors:
        try:
            page.fill(s, value, timeout=2000)
            return True
        except Exception:
            continue
    return False

def try_submit(page) -> bool:
    # 常见提交方式
    tries = [
        'button[type="submit"]',
        'input[type="submit"]',
    ]
    for sel in tries:
        try:
            page.click(sel, timeout=1500)
            return True
        except Exception:
            pass
    # 按文案
    for label in ["login", "sign in", "verify", "continue", "确认", "登录", "提交"]:
        try:
            page.get_by_role("button", name=re.compile(label, re.I)).click(timeout=1500)
            return True
        except Exception:
            pass
        try:
            page.locator(f'button:has-text("{label}")').first.click(timeout=1500)
            return True
        except Exception:
            pass
    # 回车
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False

def detect_2fa(page) -> bool:
    # 粗略检查 2FA/TOTP 输入框或提示文案
    candidates = [
        'input[name="totp"]', '#totp', 'input[name*="otp"]', 'input[name*="2fa"]',
        'input[name*="token"]', 'input[name*="code"]',
    ]
    for sel in candidates:
        try:
            if page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:
            pass
    try:
        if page.get_by_text(re.compile(r"Two[-\s]?Factor|2FA|Authenticator|验证码|动态口令", re.I)).first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    return False

def handle_2fa(page, totp_secret: Optional[str]) -> bool:
    if not detect_2fa(page):
        return True  # 无 2FA
    if not totp_secret:
        log("检测到两步验证页面，但未提供 GTX_TOTP_SECRET，无法继续自动登录。")
        return False
    if not pyotp:
        log("检测到两步验证页面，但未安装 pyotp。")
        return False
    try:
        code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
        # 填入可能的输入框
        candidates = [
            'input[name="totp"]', '#totp', 'input[name*="otp"]', 'input[name*="2fa"]',
            'input[name*="token"]', 'input[name*="code"]',
        ]
        if not find_and_fill(page, candidates, code):
            log("未找到 2FA 验证码输入框。")
            return False
        if not try_submit(page):
            log("2FA 提交失败。")
            return False
        page.wait_for_load_state("networkidle", timeout=15000)
        return True
    except Exception as e:
        log(f"2FA 处理异常：{e}")
        return False

def ensure_login(page, base_url: str, login_path: str, email: Optional[str], password: Optional[str], totp_secret: Optional[str]) -> str:
    """
    返回状态：ok / captcha / fail
    """
    page.goto(f"{base_url}{login_path}", wait_until="domcontentloaded")
    dump_debug(page, "login-page")
    if login_path not in page.url:
        log("检测到已登录（cookie 生效）")
        return "ok"

    if has_captcha(page):
        log("检测到验证码/Cloudflare 安全页，建议使用 cookie 登录。")
        dump_debug(page, "captcha-blocked")
        return "captcha"

    if not (email and password):
        log("未提供邮箱/密码，且 cookie 无效，无法继续。")
        return "fail"

    log("使用邮箱/密码尝试登录...")
    if not find_and_fill(page, ['input[name="email"]', 'input[type="email"]', '#email', 'input[name="username"]'], email):
        log("找不到邮箱输入框。")
        return "fail"
    if not find_and_fill(page, ['input[name="password"]', 'input[type="password"]', '#password'], password):
        log("找不到密码输入框。")
        return "fail"

    try_submit(page)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass

    if has_captcha(page):
        log("登录提交后出现验证码/Cloudflare 安全页，改用 cookie。")
        dump_debug(page, "captcha-after-submit")
        return "captcha"

    # 两步验证
    if detect_2fa(page):
        log("检测到两步验证页面，尝试输入 TOTP 验证码...")
        if not handle_2fa(page, totp_secret):
            dump_debug(page, "2fa-failed")
            return "fail"
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass

    if login_path in page.url:
