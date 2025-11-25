# gtx_auto_extend.py
import os
import re
import sys
import json
import time
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    print(f"[{now()}] {msg}", flush=True)

def load_cookies(path: Path) -> List[dict]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as e:
            log(f"读取 cookies 失败，将忽略：{e}")
    return []

def save_cookies(path: Path, cookies: List[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")

def has_captcha(page) -> bool:
    # 粗略检测 hCaptcha/reCAPTCHA
    try:
        if page.locator('iframe[src*="hcaptcha"], iframe[src*="recaptcha"]').first.is_visible(timeout=2000):
            return True
    except Exception:
        pass
    return False

def ensure_login(page, base_url: str, email: Optional[str], password: Optional[str]) -> bool:
    # 访问登录页（若 cookie 有效会自动跳转到面板）
    page.goto(f"{base_url}/auth/login", wait_until="domcontentloaded")
    # 如果已经不在登录页，视为已登录
    if "/auth/login" not in page.url:
        log("检测到已登录（cookie 生效）")
        return True

    if has_captcha(page):
        log("检测到验证码（captcha），无法自动登录。请先手动登录一次生成 cookies。")
        return False

    # 没有登录且没有兜底凭据
    if not (email and password):
        log("未提供邮箱/密码，且 cookie 无效，无法继续。")
        return False

    # 填写表单
    log("使用邮箱/密码尝试登录...")
    # 常见登录字段名
    candidates_email = [
        'input[name="email"]', 'input[type="email"]', '#email', 'input[name="username"]'
    ]
    candidates_pwd = [
        'input[name="password"]', 'input[type="password"]', '#password'
    ]
    filled = False
    for sel in candidates_email:
        try:
            page.fill(sel, email, timeout=3000)
            filled = True
            break
        except Exception:
            continue
    if not filled:
        log("找不到邮箱输入框，可能页面结构有变。")
        return False

    filled = False
    for sel in candidates_pwd:
        try:
            page.fill(sel, password, timeout=3000)
            filled = True
            break
        except Exception:
            continue
    if not filled:
        log("找不到密码输入框，可能页面结构有变。")
        return False

    # 提交
    # 常见提交方式：button[type=submit] 或 文本包含 "Login"/"Sign In"
    submitted = False
    try:
        page.click('button[type="submit"]', timeout=3000)
        submitted = True
    except Exception:
        pass
    if not submitted:
        try:
            page.get_by_role("button", name=re.compile("login|sign in", re.I)).click(timeout=3000)
            submitted = True
        except Exception:
            pass
    if not submitted:
        try:
            page.press('input[name="password"]', "Enter", timeout=2000)
            submitted = True
        except Exception:
            pass

    if not submitted:
        log("未能提交登录表单。")
        return False

    # 等待跳转或面板元素出现
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass

    if "/auth/login" in page.url:
        # 检查错误提示
        page_text = page.content()
        if re.search(r"invalid|incorrect|失败|错误|不正确", page_text, re.I):
            log("登录失败：账号或密码可能不正确。")
        else:
            log("登录可能失败（仍停留在登录页）。")
        return False

    log("登录成功。")
    return True

def goto_server_manage(page, base_url: str, server_url: Optional[str], server_name: Optional[str], server_index: int) -> bool:
    # 如果提供了精准服务器 URL，直接访问
    if server_url:
        log(f"跳转到指定服务器页面：{server_url}")
        page.goto(server_url, wait_until="domcontentloaded")
        return True

    # 先去仪表盘或服务器列表页
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # 优先找 “Manage Server”
    try:
        link = page.get_by_role("link", name=re.compile(r"Manage\s*Server", re.I)).nth(server_index)
        link.wait_for(state="visible", timeout=8000)
        link.click()
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        log("已进入服务器管理页（通过 Manage Server 按钮）。")
        return True
    except Exception:
        pass

    # 若提供了服务器名称，尝试按名称匹配后就近点击“Manage”
    if server_name:
        try:
            # 找到含服务器名的卡片，再找其中的 Manage/Manage Server
            card = page.locator(f"text=/{re.escape(server_name)}/i").first
            card.wait_for(state="visible", timeout=8000)
            # 在该区域内找按钮/链接
            try:
                card.get_by_role("link", name=re.compile(r"Manage", re.I)).click(timeout=3000)
            except Exception:
                card.locator('a:has-text("Manage")').first.click(timeout=3000)
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            log(f"已进入服务器管理页（匹配服务器名 {server_name}）。")
            return True
        except Exception:
            pass

    # 兜底：寻找 /server/ 的链接
    try:
        a = page.locator('a[href*="/server/"]').nth(server_index)
        a.wait_for(state="visible", timeout=8000)
        a.click()
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        log("已进入服务器管理页（通过 /server/ 链接）。")
        return True
    except Exception:
        pass

    log("未能定位到服务器管理页入口（Manage Server）。")
    return False

def click_extend(page) -> str:
    # 返回状态：extended / already / unknown
    patterns = [
        re.compile(r"EXTEND\s*72\s*HOUR", re.I),
        re.compile(r"EXTEND\s*72", re.I),
        re.compile(r"EXTEND.*72", re.I),
    ]

    # 找按钮
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
        # 再兜底一次：直接从文案定位
        try:
            btn = page.locator('text=/EXTEND\\s*72\\s*HOUR/i').first
            btn.wait_for(state="visible", timeout=5000)
        except Exception:
            pass

    if not btn:
        log('未找到 "EXTEND 72 HOUR(S)" 按钮，页面可能变化。')
        return "unknown"

    log('点击 "EXTEND 72 HOUR(S)"...')
    try:
        btn.click(timeout=5000)
    except Exception as e:
        log(f"点击失败：{e}")
        return "unknown"

    # 等待结果提示（toast/alert/modal），最多 10 秒
    success_keys = [
        r"extended", r"success", r"已续期", r"已延长", r"72", r"小时"
    ]
    already_keys = [
        r"already\s*extended", r"once\s*per\s*day", r"已续过", r"每天只能续期一次"
    ]
    text = ""
    for _ in range(20):
        time.sleep(0.5)
        try:
            # 聚合页面文本提示
            potential = page.locator('.alert, [role="alert"], .toast, .swal2-popup, .modal, .message, .notification')
            count = potential.count()
            if count > 0:
                texts = []
                for i in range(min(count, 8)):
                    try:
                        t = potential.nth(i).inner_text(timeout=500)
                        if t:
                            texts.append(t.strip())
                    except Exception:
                        pass
                text = "\n".join(texts)
                if text:
                    break
        except Exception:
            pass

    combined = (text or "") + "\n" + (page.inner_text("body")[:5000] if page else "")
    if re.search("|".join(already_keys), combined, re.I):
        return "already"
    if re.search("|".join(success_keys), combined, re.I):
        return "extended"

    return "unknown"

def main():
    base_url = os.getenv("GTX_BASE_URL", "https://gamepanel2.gtxgaming.co.uk").rstrip("/")
    login_path = os.getenv("GTX_LOGIN_PATH", "/auth/login")
    email = os.getenv("GTX_EMAIL")
    password = os.getenv("GTX_PASSWORD")
    cookie_path = Path(os.getenv("GTX_COOKIE_PATH", ".cache/cookies.json"))
    server_url = os.getenv("GTX_SERVER_URL")  # 可指定具体服务器页，优先级最高
    server_name = os.getenv("GTX_SERVER_NAME")  # 或指定服务器名称
    server_index = int(os.getenv("GTX_SERVER_INDEX", "0"))
    headless = os.getenv("GTX_HEADLESS", "1") != "0"
    timeout_ms = int(os.getenv("GTX_TIMEOUT_MS", "30000"))

    log(f"目标面板：{base_url}{login_path}")
    cookies = load_cookies(cookie_path)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-gpu",
        ])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            locale="en-US",
        )
        context.set_default_timeout(timeout_ms)

        if cookies:
            try:
                context.add_cookies(cookies)
                log(f"已加载本地 cookies（{len(cookies)} 条）")
            except Exception as e:
                log(f"导入 cookies 失败：{e}")

        page = context.new_page()

        # 登录
        ok = ensure_login(page, base_url, email, password)
        if not ok:
            log("登录失败。")
            context.close(); browser.close()
            # 若是验证码导致失败，不当作硬错误，让 Actions 不要变红
            if has_captcha(page):
                sys.exit(0)
            sys.exit(2)

        # 进入服务器管理页
        ok = goto_server_manage(page, base_url, server_url, server_name, server_index)
        if not ok:
            log("未能进入服务器管理页。")
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
            rc = 0  # 不把它当错误，避免打扰；如需严格失败可改为 2

        # 保存 cookies（可减少后续登录频率）
        try:
            latest = context.cookies()
            save_cookies(cookie_path, latest)
            log(f"已保存 cookies（{len(latest)} 条）到 {cookie_path}")
        except Exception as e:
            log(f"保存 cookies 失败：{e}")

        context.close()
        browser.close()
        sys.exit(rc)

if __name__ == "__main__":
    main()
