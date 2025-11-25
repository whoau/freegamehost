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
        # 仍在登录页，提取错误提示
        content = page.content()
        dump_debug(page, "login-still-on-page")
        # 如果是安全/验证码，不当作密码错误
        if re.search(r"captcha|security|verify you are human|cloudflare", content, re.I):
            log("被安全校验拦截（非账号错误）。")
            return "captcha"
        if re.search(r"invalid|incorrect|错误|不正确|失败", content, re.I):
            log("登录失败：账号或密码可能不正确，或被安全页拦截。")
        else:
            log("登录未成功，仍停留在登录页。")
        return "fail"

    log("登录成功。")
    dump_debug(page, "login-success")
    return "ok"

def goto_server_manage(page, base_url: str, server_url: Optional[str], server_name: Optional[str], server_index: int) -> bool:
    if server_url:
        log(f"跳转到指定服务器页面：{server_url}")
        page.goto(server_url, wait_until="domcontentloaded")
        dump_debug(page, "server-page")
        return True

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    dump_debug(page, "dashboard")
    # 直接找“Manage Server”
    try:
        link = page.get_by_role("link", name=re.compile(r"Manage\s*Server", re.I)).nth(server_index)
        link.wait_for(state="visible", timeout=8000)
        link.click()
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        dump_debug(page, "server-page")
        log("已进入服务器管理页（通过 Manage Server 按钮）。")
        return True
    except Exception:
        pass

    if server_name:
        try:
            card = page.locator(f"text=/{re.escape(server_name)}/i").first
            card.wait_for(state="visible", timeout=8000)
            try:
                card.get_by_role("link", name=re.compile(r"Manage", re.I)).click(timeout=3000)
            except Exception:
                card.locator('a:has-text("Manage")').first.click(timeout=3000)
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            dump_debug(page, "server-page")
            log(f"已进入服务器管理页（匹配服务器名 {server_name}）。")
            return True
        except Exception:
            pass

    try:
        a = page.locator('a[href*="/server/"]').nth(server_index)
        a.wait_for(state="visible", timeout=8000)
        a.click()
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        dump_debug(page, "server-page")
        log("已进入服务器管理页（通过 /server/ 链接）。")
        return True
    except Exception:
        pass

    log("未能定位到服务器管理页入口（Manage Server）。")
    dump_debug(page, "server-not-found")
    return False

def click_extend(page) -> str:
    """
    返回：extended / already / unknown
    """
    patterns = [
        re.compile(r"EXTEND\s*72\s*HOUR", re.I),
        re.compile(r"EXTEND\s*72", re.I),
        re.compile(r"EXTEND.*72", re.I),
    ]

    btn = None
    for pat in patterns:
        try:
            btn = page.get_by_role("button", name=pat)
            btn.wait_for(state="visible", timeout=5000)
            break
        except Exception:
            try:
                btn = page.get_by_role("link", name=pat)
                btn.wait_for(state="visible", timeout=5000)
                break
            except Exception:
                pass
        try:
            btn = page.get_by_text(pat)
            btn.wait_for(state="visible", timeout=5000)
            break
        except Exception:
            pass

    if not btn:
        try:
            btn = page.locator('text=/EXTEND\\s*72\\s*HOUR/i').first
            btn.wait_for(state="visible", timeout=5000)
        except Exception:
            pass

    if not btn:
        log('未找到 "EXTEND 72 HOUR(S)" 按钮，页面可能变化。')
        dump_debug(page, "extend-button-missing")
        return "unknown"

    log('点击 "EXTEND 72 HOUR(S)"...')
    try:
        btn.click(timeout=5000)
    except Exception as e:
        log(f"点击失败：{e}")
        dump_debug(page, "extend-click-failed")
        return "unknown"

    # 可能有确认弹窗
    time.sleep(1)
    for label in ["Yes", "Confirm", "OK", "确定", "是"]:
        try:
            page.get_by_role("button", name=re.compile(label, re.I)).click(timeout=1500)
            break
        except Exception:
            pass

    # 等待提示
    success_keys = [
        r"\bextended\b", r"success", r"已续期", r"已延长", r"72", r"小时"
    ]
    already_keys = [
        r"already\s*extended", r"once\s*per\s*day", r"已续过", r"每天只能续期一次",
        r"You have already extended\s*your.*server today",
    ]
    text = ""
    for _ in range(20):
        time.sleep(0.5)
        try:
            potential = page.locator('.alert, [role="alert"], .toast, .swal2-popup, .modal, .message, .notification')
            count = potential.count()
            if count > 0:
                texts = []
                for i in range(min(count, 8)):
                    try:
                        t = potential.nth(i).inner_text(timeout=400)
                        if t:
                            texts.append(t.strip())
                    except Exception:
                        pass
                text = "\n".join(texts)
                if text:
                    break
        except Exception:
            pass

    body_text = ""
    try:
        body_text = page.inner_text("body")[:8000]
    except Exception:
        pass
    combined = (text or "") + "\n" + (body_text or "")
    if re.search("|".join(already_keys), combined, re.I):
        dump_debug(page, "extend-already")
        return "already"
    if re.search("|".join(success_keys), combined, re.I):
        dump_debug(page, "extend-success")
        return "extended"

    dump_debug(page, "extend-unknown")
    return "unknown"

def main():
    base_url = os.getenv("GTX_BASE_URL", "https://gamepanel2.gtxgaming.co.uk").rstrip("/")
    login_path = os.getenv("GTX_LOGIN_PATH", "/auth/login")
    email = os.getenv("GTX_EMAIL")
    password = os.getenv("GTX_PASSWORD")
    totp_secret = os.getenv("GTX_TOTP_SECRET")  # 可选：两步验证 TOTP Base32
    cookie_path = Path(os.getenv("GTX_COOKIE_PATH", ".cache/cookies.json"))
    server_url = os.getenv("GTX_SERVER_URL")  # 可选：指定具体服务器管理页
    server_name = os.getenv("GTX_SERVER_NAME")  # 可选：按名称匹配
    server_index = int(os.getenv("GTX_SERVER_INDEX", "0"))
    headless = os.getenv("GTX_HEADLESS", "1") != "0"
    timeout_ms = int(os.getenv("GTX_TIMEOUT_MS", "40000"))

    log(f"目标面板：{base_url}{login_path}")

    # 预加载 cookies（缓存文件 + 环境变量）
    cookies = load_cookies_file(cookie_path)
    seeded = seed_cookies_from_env(domain_default="gamepanel2.gtxgaming.co.uk")
    # 去重合并
    def key(c): return (c.get("domain",""), c.get("path","/"), c.get("name",""))
    merged = { key(c): c for c in cookies }
    for c in seeded:
        merged[key(c)] = c
    cookies = list(merged.values())
    if cookies:
        log(f"准备导入 cookies（{len(cookies)} 条）")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-gpu",
        ])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            locale="en-US",
        )
        context.set_default_timeout(timeout_ms)

        if cookies:
            try:
                context.add_cookies(cookies)
                log("已导入 cookies（可能直接免登）")
            except Exception as e:
                log(f"导入 cookies 失败：{e}")

        page = context.new_page()

        # 登录
        status = ensure_login(page, base_url, login_path, email, password, totp_secret)
        if status == "fail":
            log("登录失败。")
            dump_debug(page, "login-fail")
            context.close(); browser.close()
            sys.exit(2)
        if status == "captcha":
            log("因验证码/安全校验无法自动登录。本次不视为错误（退出 0）。建议设置 GTX_COOKIE_HEADER 或手动跑一次缓存 cookies。")
            dump_debug(page, "login-captcha")
            # 也尝试保存当前 cookies（若有）
            try:
                latest = context.cookies()
                if latest:
                    save_cookies(cookie_path, latest)
            except Exception:
                pass
            context.close(); browser.close()
            sys.exit(0)

        # 进入服务器管理页
        if not goto_server_manage(page, base_url, server_url, server_name, server_index):
            log("未能进入服务器管理页。")
            dump_debug(page, "server-enter-fail")
            context.close(); browser.close()
            sys.exit(2)

        # 点击续期
        status = click_extend(page)
        if status == "extended":
            log("续期成功 ✅")
            rc = 0
        elif status == "already":
            log("今天已经续过，跳过 ✅（不报错）")
            rc = 0
        else:
            log("未能确认续期是否成功，可能页面结构变化或需要人工确认。")
            rc = 0  # 默认不标红；如需严格失败将此改为 2

        # 保存 cookies（减少后续登录）
        try:
            latest = context.cookies()
            if latest:
                save_cookies(cookie_path, latest)
        except Exception as e:
            log(f"保存 cookies 失败：{e}")

        context.close()
        browser.close()
        sys.exit(rc)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"未捕获异常：{e}")
        traceback.print_exc()
        if DEBUG:
            try:
                ART_DIR.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        sys.exit(2)
