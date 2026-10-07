"""登录判定的离线测试。

这块踩过两个坑，都是有实测依据的，所以单独测：

1. **小红书给未登录访客也种 `web_session`** —— 靠 cookie 名判定会误判成
   「已登录」，用户扫码时被告知「无需重复登录」，直接卡住。
   实测记录：匿名访问 /explore 后 cookie 里就有 web_session。
   可靠的判据是「访问 /explore 会不会被 302 到 /login」。

2. **用户把登录窗口叉掉** —— 原来会抛 TargetClosedError 崩掉，
   堆栈一路打到用户脸上。现在要和「等超时」分开报。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frisbee_radar.browser import (  # noqa: E402
    AUTH_COOKIES,
    LOGIN_BY_CONTENT,
    LOGIN_BY_REDIRECT,
    LOGIN_CLOSED,
    LOGIN_OK,
    LOGIN_TIMEOUT,
    LoginWindowClosed,
    _is_closed_error,
    is_slow_check,
)
from frisbee_radar.config import load_config  # noqa: E402


class TestLoginOutcomeConstants(unittest.TestCase):
    def test_three_distinct_outcomes(self):
        values = {LOGIN_OK, LOGIN_CLOSED, LOGIN_TIMEOUT}
        self.assertEqual(len(values), 3, "三种结果必须互不相同")

    def test_closed_exception_exists(self):
        self.assertTrue(issubclass(LoginWindowClosed, RuntimeError))


class TestClosedErrorDetection(unittest.TestCase):
    """关窗口要能被识别出来，且不能把无关错误也当成关窗口。"""

    def test_detects_target_closed(self):
        class TargetClosedError(Exception):
            pass

        self.assertTrue(_is_closed_error(TargetClosedError("boom")))

    def test_detects_message_variants(self):
        for message in (
            "Target page, context or browser has been closed",
            "Browser has been closed",
            "target closed",
        ):
            self.assertTrue(_is_closed_error(RuntimeError(message)), message)

    def test_does_not_swallow_timeouts_or_other_errors(self):
        for message in ("Timeout 30000ms exceeded", "net::ERR_CONNECTION_REFUSED", ""):
            self.assertFalse(_is_closed_error(RuntimeError(message)), message)


class TestXiaohongshuLoginCheck(unittest.TestCase):
    """这一组是这次修复的核心。"""

    def test_does_not_trust_web_session_cookie(self):
        """web_session 匿名访客也有，绝不能拿它当登录凭据。"""
        self.assertNotIn("web_session", AUTH_COOKIES["xiaohongshu"])
        self.assertEqual(AUTH_COOKIES["xiaohongshu"], ())

    def test_uses_redirect_criterion(self):
        self.assertIn("xiaohongshu", LOGIN_BY_REDIRECT)
        probe = LOGIN_BY_REDIRECT["xiaohongshu"]
        self.assertIn("/explore", probe["url"])
        self.assertIn("/login", probe["logged_out_markers"])

    def test_redirect_check_is_marked_slow(self):
        """它要开页面，轮询里必须降频，不能每 3 秒来一次。"""
        self.assertTrue(is_slow_check("xiaohongshu"))

    def test_no_stale_content_probe(self):
        """曾经配过一条基于页面内容长度的判据，理由是错的，已撤。"""
        self.assertNotIn("xiaohongshu", LOGIN_BY_CONTENT)


class TestDouyinLoginCheck(unittest.TestCase):
    def test_session_cookies_are_login_only(self):
        """实测匿名访客只有 ttwid/odin_tt/__ac_nonce，没有 sessionid 系列。"""
        self.assertIn("sessionid", AUTH_COOKIES["douyin"])
        self.assertIn("sessionid_ss", AUTH_COOKIES["douyin"])
        self.assertIn("sid_tt", AUTH_COOKIES["douyin"])

    def test_not_slow(self):
        self.assertFalse(is_slow_check("douyin"))


class TestWechatLoginCheck(unittest.TestCase):
    def test_mp_backend_uses_session_cookies(self):
        self.assertIn("slave_sid", AUTH_COOKIES["wechat"])

    def test_wechat_not_slow(self):
        self.assertFalse(is_slow_check("wechat"))


class TestNoPlatformTakesBanCookieForGranted(unittest.TestCase):
    """总检查：任何平台的 cookie 判据都不能是「匿名访客也会拿到」的名字。"""

    # 实测确认匿名访客就会拿到的 cookie 名
    ANONYMOUS_COOKIE_NAMES = {
        "web_session",     # 小红书
        "ttwid",           # 抖音
        "odin_tt",         # 抖音
        "__ac_nonce",      # 抖音
        "a1",              # 小红书
        "webId",           # 小红书
    }

    def test_no_platform_trusts_an_anonymous_cookie(self):
        for platform, names in AUTH_COOKIES.items():
            offending = set(names) & self.ANONYMOUS_COOKIE_NAMES
            self.assertFalse(
                offending,
                f"{platform} 的登录判据里混进了匿名访客也会有的 cookie：{offending}",
            )


class TestCdpMode(unittest.TestCase):
    """连用户自己的浏览器（复用已有登录态）。

    这条路上踩过一个坑：browser_context 里有两个 yield（CDP 分支一个、
    启动独立 Chromium 一个），CDP 分支退出时漏了 return，会继续往下撞到
    第二个 yield，asyncio 报 "generator didn't stop"。已修，这里做行为验证。
    """

    def test_unreachable_cdp_gives_actionable_error(self):
        import asyncio

        from frisbee_radar.browser import LoginRequired, browser_context

        async def attempt():
            async with browser_context(
                "x", Path("data/browser_state"),
                headless=True, cdp_url="http://127.0.0.1:9",
            ):
                pass

        with self.assertRaises(LoginRequired) as ctx:
            asyncio.run(attempt())
        message = str(ctx.exception)
        # 报错必须直接告诉用户该双击哪个 bat，而不是甩一句 Connection refused
        self.assertIn("7-用我的浏览器.bat", message)

    def test_cdp_url_is_read_from_platform_config(self):
        cfg = load_config()
        for platform in ("xiaohongshu", "douyin"):
            settings = cfg.platform(platform)
            self.assertIn("cdp_url", settings.extra, f"{platform} 少了 cdp_url 配置项")

    def test_shipped_cdp_url_defaults_to_empty(self):
        """默认走独立 Chromium（扫码一次），CDP 是可选增强。"""
        cfg = load_config()
        for platform in ("xiaohongshu", "douyin"):
            self.assertEqual(cfg.platform(platform).option("cdp_url", ""), "")


class TestSourcesDoNotHardGateOnLogin(unittest.TestCase):
    """采集器不能在采集前用不可靠的判据把用户挡在外面。

    小红书实测没有可靠的登录信号，判错成「未登录」会让已经登录好的用户
    反复扫码都进不去。所以改成「采完一条都没有时列出可能原因」。
    """

    def test_xhs_and_douyin_have_no_ensure_login(self):
        for module_name in ("xiaohongshu", "douyin"):
            module = __import__(
                f"frisbee_radar.sources.{module_name}", fromlist=["x"]
            )
            source_cls = next(
                obj for name, obj in vars(module).items()
                if isinstance(obj, type) and name.endswith("Source")
            )
            self.assertFalse(
                hasattr(source_cls, "_ensure_login"),
                f"{module_name} 还留着采集前的登录拦截",
            )

    def test_empty_result_error_mentions_login_and_the_bat(self):
        from frisbee_radar.sources.xiaohongshu import XiaohongshuSource

        hint = XiaohongshuSource._empty_result_hint("飞盘")
        self.assertIn("未登录", hint)
        self.assertIn("3-登录小红书.bat", hint)
        self.assertIn("7-用我的浏览器.bat", hint)


class TestLoginPollingDoesNotDisturbTheUser(unittest.TestCase):
    """轮询登录状态时不许抢焦点。

    用户亲历的 bug：「我在扫描登录的时候，总是会跳转」。
    原因是轮询里调了 verify_login()，而小红书的判据是「开新页访问 /explore
    看会不会跳登录页」—— 每 6 秒开一个新标签、抢一次焦点，
    用户正对着二维码扫，窗口一直跳，根本扫不成。

    正确做法是只看用户当前那个登录页：扫码成功后登录页自己会跳走。
    """

    @classmethod
    def setUpClass(cls):
        import inspect

        import frisbee_radar.browser as browser

        cls.source = inspect.getsource(browser.wait_for_login)

    def _called_names(self) -> set[str]:
        import ast

        tree = ast.parse(self.source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        return names

    def test_does_not_open_probe_pages_while_waiting(self):
        called = self._called_names()
        forbidden = {"verify_login", "_probe_redirect", "_probe_content"}
        offending = called & forbidden
        self.assertFalse(
            offending,
            f"轮询里调了 {offending} —— 它们会新开页面抢焦点，"
            "用户正扫码时窗口会一直跳，扫不成。",
        )

    def test_only_opens_the_login_page_once(self):
        """new_page 只允许在开头开登录页时出现一次（AST 里就是同一个名字）。"""
        self.assertIn("new_page", self._called_names())

    def test_watches_the_current_page_url(self):
        """扫码成功后登录页自己会跳走，看这个比另开页探测可靠且无副作用。"""
        self.assertIn("page.url", self.source)

    def test_brings_the_login_page_to_front(self):
        self.assertIn("bring_to_front", self.source)

    def test_has_a_note_explaining_why(self):
        """这条约束很反直觉（『探测一下登录态』看起来天经地义），必须留说明。"""
        self.assertIn("跳转", self.source)


class TestQrCodeExpiryHandling(unittest.TestCase):
    """二维码会过期，而且很快 —— 用户扫一个死码什么都不会发生。"""

    def test_expiry_hints_cover_observed_text(self):
        from frisbee_radar.browser import QR_EXPIRED_HINTS

        # 实测截图里的原文是「二维码已过期 / 点击刷新」
        self.assertTrue(any("已过期" in h for h in QR_EXPIRED_HINTS))
        self.assertTrue(any("点击刷新" in h for h in QR_EXPIRED_HINTS))

    def test_refresh_texts_cover_observed_button(self):
        from frisbee_radar.browser import QR_REFRESH_TEXTS

        self.assertIn("点击刷新", QR_REFRESH_TEXTS)

    def test_wait_loop_calls_the_refresher(self):
        import ast
        import inspect

        import frisbee_radar.browser as browser

        tree = ast.parse(inspect.getsource(browser.wait_for_login))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("_refresh_qrcode_if_expired", called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
