"""Playwright 会话与登录态管理。

这是整个项目的技术核心，思路和 MediaCrawler 一致但更精简：

    不做 JS 逆向，而是用真实浏览器登录一次，把登录态（cookie +
    localStorage）持久化到本地目录。之后每次采集都复用这个目录，
    平台看到的就是一个已登录的正常浏览器。

为什么不用纯 HTTP 请求 + 签名算法：
    小红书要 x-s/x-t，抖音要 a_bogus，这些签名算法由平台前端 JS
    动态生成且频繁更换。与其每次改版都去逆向，不如让浏览器自己算。
    签名参数直接由浏览器发出，我们只在旁边监听响应。

代价是需要装 Chromium（约 130MB），且采集时必须跑一个（可以是
headless 的）浏览器进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

# 平台 -> 登录页
#
# ⚠️ 视频号那条是「视频号助手」——创作者后台，只能管自己的号，
# 没有公开的内容搜索页。也就是说视频号在网页端做不了关键词采集，
# 详见 frisbee_radar/sources/wechat_channels.py 的说明。
# 这里保留地址只是为了让 login 命令能开出一个有二维码的页面，
# 而不是一片空白。
LOGIN_URLS: dict[str, str] = {
    "xiaohongshu": "https://www.xiaohongshu.com/explore",
    "douyin": "https://www.douyin.com/",
    "wechat": "https://mp.weixin.qq.com/",
    "wechat_channels": "https://channels.weixin.qq.com/login.html",
}

# 平台 -> 判断登录是否成功的 cookie 名
#
# ⚠️ 小红书故意留空。实测它给**未登录访客也种 web_session**，
# 靠 cookie 名会误判成「已登录」，然后用户扫码时被告知「无需重复登录」而卡住。
# 它改用 LOGIN_BY_REDIRECT 里的跳转判据，那个才可靠。
AUTH_COOKIES: dict[str, tuple[str, ...]] = {
    "xiaohongshu": (),
    # 抖音实测匿名访客只有 ttwid / odin_tt / __ac_nonce 这些，
    # 没有 sessionid/sessionid_ss/sid_tt —— 这三个确实是登录后才有的
    "douyin": ("sessionid", "sessionid_ss", "sid_tt"),
    "wechat": ("slave_sid", "bizuin"),
    "wechat_channels": ("sessionid", "wxuin", "sessionid_channel", "finder_sessionid"),
}

# 平台 -> 用「访问内容页会不会被踢到登录页」判定。比 cookie 可靠。
#
# 实测（2026-10-06）：小红书未登录访问 /explore 会 302 到
# /login?redirectPath=...；登录后停在 /explore。这个差异骗不了人。
LOGIN_BY_REDIRECT: dict[str, dict] = {
    "xiaohongshu": {
        "url": "https://www.xiaohongshu.com/explore",
        "logged_out_markers": ("/login",),
        "settle_sec": 4,
    },
}

# 平台 -> 用「页面有没有渲染出内容」判定。
# 目前是空的：视频号本来配过一条，后来发现它的页面未登录时也是空壳，
# 拿它当判据是错的，已撤掉。机制留着备用。
LOGIN_BY_CONTENT: dict[str, dict] = {}


class LoginWindowClosed(RuntimeError):
    """登录窗口被用户关掉了（不是超时，也不是失败）。"""


def _is_closed_error(exc: BaseException) -> bool:
    """判断异常是不是「浏览器/页面被关掉了」。"""
    if "TargetClosed" in type(exc).__name__:
        return True
    text = str(exc).lower()
    return "has been closed" in text or "target closed" in text

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class LoginRequired(RuntimeError):
    """登录态缺失或已过期。"""


def state_dir(base: Path, platform: str) -> Path:
    path = base / platform
    path.mkdir(parents=True, exist_ok=True)
    return path


def _import_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise RuntimeError(
            "缺少 playwright。请先执行：\n"
            "    .venv\\Scripts\\pip install playwright\n"
            "    .venv\\Scripts\\playwright install chromium"
        ) from exc
    return async_playwright


@asynccontextmanager
async def browser_context(
    platform: str,
    base_dir: Path,
    headless: bool = True,
    slow_mo: int = 0,
    cdp_url: str = "",
) -> AsyncIterator[Any]:
    """打开一个浏览器上下文。

    两条路径：

    1. **cdp_url 为空（默认）**：启动本项目的独立 Chromium，
       登录态存在 data/browser_state/<平台>/ 里。扫码一次，之后复用。

    2. **cdp_url 给了**（形如 http://127.0.0.1:9222）：连到你**自己**开着的
       浏览器上，直接复用你平时那个浏览器的登录态 —— 不用扫码。
       这是「启动带调试端口的浏览器.bat」配合用的模式，跟你 X 那套一样。

    ⚠️ 走 CDP 时绝对不能 close 浏览器 —— 那是用户的浏览器，关掉会把他
       所有标签页一起干掉。所以这里只断开连接，不动它。

    ⚠️ 同一个 user_data_dir 不能被两个进程同时打开。所以默认模式
       （启动独立 Chromium）不要并发跑同一个平台。
    """
    async_playwright = _import_playwright()

    # ---- 模式 2：连用户自己的浏览器 ----
    if cdp_url:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.connect_over_cdp(cdp_url)
            except Exception as exc:
                raise LoginRequired(
                    f"连不上 {cdp_url} 上的浏览器。\n"
                    "请先双击「7-用我的浏览器.bat」把带调试端口的浏览器启动起来，\n"
                    "再跑采集/登录命令。原始错误：" + str(exc)[:160]
                ) from exc

            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = await browser.new_context()
            context.set_default_timeout(30_000)

            # 故意不调 browser.close()：那是用户的浏览器，关掉会把他所有标签页
            # 一起干掉。退出 async_playwright() 时连接自然断开就够了 ——
            # 实测浏览器仍活着。
            yield context
        # ⚠️ 这个 return 不能删：下面还有第二个 yield（启动独立 Chromium 那条路）。
        # 少了它，CDP 分支退出后会继续往下走，撞到第二个 yield，
        # asyncio 会报 "generator didn't stop"。
        return

    # ---- 模式 1：启动独立 Chromium（默认）----
    profile = state_dir(base_dir, platform)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=headless,
            slow_mo=slow_mo,
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=[
                # 关掉自动化特征里最容易触发风控的几个开关
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
            ],
        )
        context.set_default_timeout(30_000)
        try:
            yield context
        finally:
            await context.close()


async def has_login(context: Any, platform: str) -> bool:
    """快速判定：看会话 cookie 在不在。不加载页面。"""
    names = AUTH_COOKIES.get(platform, ())
    if not names:
        return False
    try:
        cookies = await context.cookies()
    except Exception:
        return False
    present = {c.get("name") for c in cookies}
    return any(n in present for n in names)


async def _probe_content(context: Any, probe: dict) -> bool:
    """慢速判定：打开页面，看有没有渲染出内容。

    注意：这个判据只对「登录后才有内容」的页面成立。视频号当初配过一条，
    后来发现它的页面未登录时也只是一个空壳，拿它当判据是错的，已撤掉。
    """
    try:
        page = await context.new_page()
    except Exception:
        return False
    try:
        await page.goto(probe["url"], wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(probe.get("settle_sec", 5))
        text = " ".join((await page.inner_text("body")).split())
        return len(text) >= probe.get("min_text_len", 20)
    except Exception:
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _probe_redirect(context: Any, probe: dict) -> bool:
    """慢速但可靠：访问内容页，看会不会被踢到登录页。

    参数 probe 里，logged_out_markers 出现在最终 URL 里就说明没登录。
    """
    try:
        page = await context.new_page()
    except Exception:
        return False
    try:
        await page.goto(probe["url"], wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(probe.get("settle_sec", 4))
        final_url = page.url
        return not any(m in final_url for m in probe["logged_out_markers"])
    except Exception:
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def verify_login(context: Any, platform: str) -> bool:
    """完整判定登录态。

    判据优先级：跳转判据 > cookie 名 > 页面内容。
    跳转判据最靠得住但慢（要开页面），所以在登录轮询里降频调用。
    """
    redirect = LOGIN_BY_REDIRECT.get(platform)
    if redirect:
        return await _probe_redirect(context, redirect)

    if await has_login(context, platform):
        return True

    probe = LOGIN_BY_CONTENT.get(platform)
    if probe:
        return await _probe_content(context, probe)
    return False


def is_slow_check(platform: str) -> bool:
    """这个平台的登录判定要不要开页面（慢）。"""
    return platform in LOGIN_BY_REDIRECT or platform in LOGIN_BY_CONTENT


# wait_for_login 的返回值：成功 / 用户关掉了窗口 / 等超时
LOGIN_OK = "ok"
LOGIN_CLOSED = "closed"
LOGIN_TIMEOUT = "timeout"


async def read_cookie_header(context: Any, domains: tuple[str, ...] = ()) -> str:
    """把浏览器 cookie 拼成 HTTP 头，便于需要时走 httpx 直连。"""
    cookies = await context.cookies()
    picked = [
        c for c in cookies
        if not domains or any(d in (c.get("domain") or "") for d in domains)
    ]
    return "; ".join(f"{c['name']}={c['value']}" for c in picked)


# 二维码过期的页面提示。实测小红书二维码有效期很短，用户还没扫就过期了 ——
# 扫一个失效的码什么都不会发生，用户只会觉得「没反应」。所以轮询时看到这些
# 字样就自动点一次刷新，保证随时扫都是有效的。
QR_EXPIRED_HINTS = ("二维码已过期", "二维码过期", "已失效", "点击刷新")

# 点这些按钮来刷新二维码
QR_REFRESH_TEXTS = ("点击刷新", "刷新", "重新获取")


async def _safe_screenshot(page: Any, path: Path) -> bool:
    """截图，失败就算了。

    必须给 timeout：登录页上常有二维码 spinner / 轮播之类的动画，Playwright
    的 screenshot 会等页面"稳定"，等不到就默认超时 30 秒 —— 而这里每 15 秒
    就要截一张，不设超时就会互相堵住，结果一张都存不下来（实测踩过）。
    animations="disabled" 顺手把动画停掉，截出来的图也更干净。
    """
    with contextlib.suppress(Exception):
        await page.screenshot(path=str(path), timeout=8000, animations="disabled")
        return True
    return False


async def _refresh_qrcode_if_expired(page: Any) -> bool:
    """二维码过期就点一下刷新。返回是否做了刷新。

    只在这个页面内点击，不新开标签 —— 见 wait_for_login 里关于「别抢焦点」的说明。
    """
    try:
        body = await page.inner_text("body")
    except Exception:
        return False
    if not any(hint in body for hint in QR_EXPIRED_HINTS):
        return False

    for text in QR_REFRESH_TEXTS:
        try:
            locator = page.get_by_text(text, exact=False).first
            if await locator.count() > 0:
                await locator.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


async def wait_for_login(
    context: Any,
    platform: str,
    timeout_sec: int = 180,
    on_wait: Callable[[int], None] | None = None,
    debug_dir: Path | None = None,
) -> str:
    """打开登录页并轮询等待用户完成扫码，返回 LOGIN_OK / LOGIN_CLOSED / LOGIN_TIMEOUT。

    headless=False 时必须由用户手动扫码；这里只负责等和检测，
    不碰任何账号密码，也不做验证码绕过。

    debug_dir 给了的话，会定期把登录页截图存进去。用途：用户说「我扫了」
    但检测不到时，能直接看图判断是二维码没出来、二维码过期了、还是扫了
    没确认 —— 比自己盯着日志猜快得多。这个功能就是靠它发现问题才加的。

    四个实测踩出来的点：
      · 用户把窗口叉掉时必须和「等超时」分开报，否则会给出误导性提示。
      · 小红书这类平台的登录判据要开页面（慢），不能每 3 秒来一次，降频查。
      · 用户以为「在自己的浏览器里登过」就等于这里登过 —— 其实是两套 cookie。
      · **二维码会过期**。小红书尤其快，用户还没掏出手机就失效了，
        扫了完全没反应。所以每轮都检查一次「已过期」并自动点刷新。
    """
    url = LOGIN_URLS.get(platform)
    if not url:
        raise ValueError(f"未知平台：{platform}")

    try:
        page = await context.new_page()
    except Exception as exc:
        if _is_closed_error(exc):
            return LOGIN_CLOSED
        raise

    # 让登录页成为当前活动标签，用户一眼就能看到二维码
    with contextlib.suppress(Exception):
        await page.bring_to_front()

    if debug_dir is not None:
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            debug_dir = None

    refreshed = 0
    try:
        await page.goto(url, wait_until="domcontentloaded")

        # 登录弹窗/二维码要等一会儿才渲染出来
        await asyncio.sleep(2)

        # ⚠️ 轮询期间**绝对不能新开页面或切标签**。
        #
        # 这里原来是调 verify_login()，而小红书的判据是「开新页访问 /explore
        # 看会不会被踢到 /login」—— 结果每 6 秒弹一个新标签、抢一次焦点，
        # 用户正在扫码时窗口一直跳，根本扫不成。用户原话：「总是会跳转」。
        #
        # 正确做法：只看用户当前正在看的这个登录页。扫码成功后登录页自己会
        # 跳走（小红书的 /login 会跳到 redirectPath 指向的地址），
        # 观察它跳没跳就够了，不用另开页面。
        redirect_probe = LOGIN_BY_REDIRECT.get(platform)
        login_markers = redirect_probe["logged_out_markers"] if redirect_probe else ()

        waited = 0
        round_no = 0
        while waited < timeout_sec:
            round_no += 1
            try:
                # 判据一：登录页自己跳走了 = 扫码成功
                if login_markers and not any(m in page.url for m in login_markers):
                    return LOGIN_OK
                # 判据二：cookie 出现了（纯内存读取，不碰页面）
                if await has_login(context, platform):
                    return LOGIN_OK
                # 二维码过期就刷新，别让用户扫一个死码
                if await _refresh_qrcode_if_expired(page):
                    refreshed += 1
                    print(
                        f"  …二维码过期了，已自动刷新（第 {refreshed} 次），现在扫是最新的",
                        flush=True,
                    )
                # 每 20 轮（约 1 分钟）留一张现场图，方便事后判断卡在哪
                if debug_dir is not None and round_no % 20 == 0:
                    await _safe_screenshot(
                        page, debug_dir / f"{platform}-{round_no:03d}.png"
                    )
            except Exception as exc:
                if _is_closed_error(exc):
                    return LOGIN_CLOSED
                raise
            await asyncio.sleep(3)
            waited += 3
            if on_wait:
                on_wait(timeout_sec - waited)
        # 超时也留一张现场图
        if debug_dir is not None:
            await _safe_screenshot(page, debug_dir / f"{platform}-timeout.png")
        return LOGIN_TIMEOUT
    except Exception as exc:
        if _is_closed_error(exc):
            return LOGIN_CLOSED
        raise
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def goto_and_collect(
    context: Any,
    url: str,
    url_pattern: str,
    extract: Callable[[dict], list[Any]],
    *,
    max_scrolls: int = 8,
    scroll_pause: float = 2.5,
    idle_rounds: int = 2,
    limit: int = 30,
) -> list[Any]:
    """打开页面并监听匹配的 XHR 响应，边滚边收。

    这是三个平台共用的采集骨架：不去构造签名请求，而是让页面自己
    发请求，我们在响应回调里把结构化数据捞出来。

    idle_rounds 控制「连续几轮滚动没有新增就停」，避免无限滚下去。
    """
    collected: list[Any] = []
    seen: set[str] = set()

    async def on_response(response: Any) -> None:
        if url_pattern not in response.url:
            return
        try:
            payload = await response.json()
        except Exception:
            return
        try:
            items = extract(payload)
        except Exception:
            return
        for item in items:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)[:400]
            if key in seen:
                continue
            seen.add(key)
            collected.append(item)

    page = await context.new_page()
    # 回调必须是同步注册；async 回调由 playwright 调度
    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    try:
        # ⚠️ 导航超时不能把整轮采集搞死。
        #
        # 实测：小红书在**显示窗口模式**（headless=False）下 goto 会等到
        # domcontentloaded 超时（30 秒）—— 页面上有持续加载的资源，这个事件
        # 可能永远不触发。但页面其实早就可用了，内容接口也已经在发。
        # 原来这里没捕获异常，TimeoutError 一路冒到基类被吞成「没有拿到内容」，
        # 表现就是采集 0 条、还看不出为什么。
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as exc:
            if _is_closed_error(exc):
                raise
            # 超时就继续：先等一会儿让内容接口有机会发出来
            print(f"  …页面导航未完成（{type(exc).__name__}），继续尝试采集", flush=True)

        await asyncio.sleep(3)

        idle = 0
        for _ in range(max_scrolls):
            if len(collected) >= limit:
                break
            before = len(collected)
            await page.mouse.wheel(0, 2400)
            await asyncio.sleep(scroll_pause)
            if len(collected) == before:
                idle += 1
                if idle >= idle_rounds:
                    break
            else:
                idle = 0
    finally:
        try:
            await page.close()
        except Exception:
            pass

    return collected[:limit]
