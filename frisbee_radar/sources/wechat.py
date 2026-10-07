"""微信公众号采集。

两条互补的路线：

  sogou  —— 搜狗微信搜索。免登录、开箱即用，缺点是只覆盖「关键词相关性
            靠前 + 收录范围内」的文章，拿不到某个号的历史全量。

  mp     —— 登录 mp.weixin.qq.com 后台，走 searchbiz / appmsg 接口，
            可以抓指定公众号的历史文章列表（只到标题+摘要级别，
            正文要点进文章页）。

关于 mp 模式的限流（重要）：
    公众平台的 appmsg 接口有很强的频率控制。连续请求大约 10~15 次后，
    会返回「频率过快」并被封禁约 1 小时，且在此期间所有请求都失败。
    本模块默认把间隔拉到 60s 以上，并且一旦检测到 freq control 就立即
    停止而不是重试。请把它当成「低频增量同步」工具，不要当全量爬虫用。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote, urljoin

import httpx

from ..browser import browser_context, has_login, wait_for_login
from ..models import Post
from .base import BaseSource, SourceError, parse_timestamp, sleep_random, to_int

SOGOU_BASE = "https://weixin.sogou.com"
SOGOU_SEARCH = f"{SOGOU_BASE}/weixin"
MP_BASE = "https://mp.weixin.qq.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 搜狗的反爬页面特征
SOGOU_BLOCK_MARKERS = ("请输入验证码", "antispider", "用户您好，您的访问过于频繁")

# 公众号后台限流特征
MP_FREQ_MARKERS = ("频率过快", "freq control", "操作过于频繁", "-200013")


class WechatSource(BaseSource):
    platform = "wechat"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        mode = (self.settings.mode or "sogou").lower()
        if mode not in ("sogou", "mp"):
            raise SourceError(f"wechat.mode 只能是 sogou 或 mp，收到 {mode!r}")
        self.mode = mode
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ 搜狗

    async def _sogou_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Referer": SOGOU_BASE + "/",
                },
                timeout=25.0,
                follow_redirects=True,
            )
            # 先摸一下首页拿 SNUID 等 cookie，否则搜索接口直接返反爬页
            try:
                await self._client.get(SOGOU_BASE + "/")
            except httpx.HTTPError:
                pass
        return self._client

    async def _crawl_sogou(self, keyword: str, limit: int) -> list[Post]:
        client = await self._sogou_client()

        posts: list[Post] = []
        seen_ids: set[str] = set()

        # 每页 10 条，翻页拿够 limit
        for page in range(1, max(2, limit // 10 + 2)):
            # 注意：不要加 tsn 参数。搜狗已经静默废弃了这个时间范围过滤，
            # 带上它（哪怕是 tsn=0）服务端会返回一个不含结果容器的空页面，
            # 表现为「一条都抓不到」。时间筛选只能在本地做（见 README）。
            params = {"type": "2", "query": keyword, "page": str(page), "ie": "utf8"}

            try:
                resp = await client.get(SOGOU_SEARCH, params=params)
            except httpx.HTTPError as exc:
                raise SourceError(f"搜狗请求失败：{exc}") from exc

            if resp.status_code != 200:
                raise SourceError(f"搜狗返回 HTTP {resp.status_code}")

            html = resp.text
            if any(marker in html for marker in SOGOU_BLOCK_MARKERS):
                raise SourceError(
                    "搜狗触发了反爬验证。请降低频率（调大 config/sources.yaml 里的 "
                    "delay_min/delay_max），或换网络/稍后重试。"
                )

            page_posts = self._parse_sogou_html(html, keyword)
            if not page_posts:
                break

            for post in page_posts:
                if post.post_id in seen_ids:
                    continue
                seen_ids.add(post.post_id)
                posts.append(post)

            if len(posts) >= limit:
                break
            await sleep_random(self.crawl_cfg.delay_min, self.crawl_cfg.delay_max)

        # 搜狗会给跳转链接，尽量换成真实的 mp.weixin.qq.com 地址
        for post in posts[:limit]:
            if post.url.startswith(SOGOU_BASE + "/link"):
                real = await self._resolve_sogou_link(post.url)
                if real:
                    post.url = real
                    post.raw["resolved"] = True
            await sleep_random(0.6, 1.6)

        return posts[:limit]

    def _parse_sogou_html(self, html: str, keyword: str) -> list[Post]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        results: list[Post] = []

        container = soup.select_one("ul.news-list")
        if container is None:
            return results

        for item in container.select("li"):
            link = item.select_one("h3 a")
            if link is None:
                continue

            title = link.get_text(strip=True)
            href = link.get("href") or ""
            if href.startswith("/"):
                href = urljoin(SOGOU_BASE, href)

            summary_el = item.select_one("p.txt-info")
            summary = summary_el.get_text(strip=True) if summary_el else ""

            # 公众号名：.all-time-y2 是新版选择器，.account 是旧版
            account_el = item.select_one("span.all-time-y2") or item.select_one(".account")
            account = account_el.get_text(strip=True) if account_el else ""

            # 时间写在 document.write(timeConvert('1652883559')) 里
            publish = None
            for script in item.select("script"):
                m = re.search(r"timeConvert\('(\d+)'\)", script.text or "")
                if m:
                    publish = parse_timestamp(int(m.group(1)))
                    break

            # 文章 id 不能用搜狗链接里的 url 参数：同一批结果里它前 64 字符
            # 完全相同（是加密载荷的公共前缀），截断后会把整页判成同一条。
            # 公众号文章的 (号名 + 标题) 稳定且唯一，还能跨运行去重。
            digest = hashlib.md5(f"{account}|{title}".encode("utf-8")).hexdigest()[:16]
            post_id = f"sogou:{digest}"

            images = []
            img = item.select_one("img")
            if img and img.get("src"):
                images.append(urljoin(SOGOU_BASE, img["src"]))

            results.append(
                Post(
                    platform="wechat",
                    post_id=post_id,
                    title=title,
                    content=summary,
                    author=account,
                    url=href,
                    publish_time=publish,
                    images=images,
                    matched_by=keyword,
                    raw={"mode": "sogou", "keyword": keyword},
                )
            )

        return results

    async def _resolve_sogou_link(self, sogou_url: str) -> str | None:
        """把搜狗的 /link?url=... 跳转换成真实文章地址。

        搜狗返回的是一小段 JS，里面用 url += '...' 拼接真实地址。
        解析失败不影响主流程（保留搜狗链接仍可人工点开）。
        """
        client = await self._sogou_client()
        try:
            resp = await client.get(sogou_url, headers={"Referer": SOGOU_SEARCH})
        except httpx.HTTPError:
            return None

        if resp.status_code != 200:
            return None

        html = resp.text
        if "antispider" in html:
            return None

        # 优先找直接的 mp.weixin.qq.com 链接
        m = re.search(r"https?://mp\.weixin\.qq\.com/s\?[^'\"<>\\]+", html)
        if m:
            return unquote(m.group(0)).replace("&amp;", "&")

        # 回退：拼接 url += 'xxx' 片段
        parts = re.findall(r"url\s*\+=\s*'([^']*)'", html)
        if parts:
            joined = "".join(parts).replace("@", "")
            if joined.startswith("http"):
                return joined

        return None

    # ------------------------------------------------------------ 公众号后台

    async def _crawl_mp(self, target: str, kind: str, limit: int) -> list[Post]:
        """登录后台抓取。kind='account' 时 target 是公众号名。"""
        headless = bool(self.settings.option("headless", False))
        # 后台接口的限流很凶，这里强制一个更保守的下限
        min_interval = float(self.settings.option("mp_min_interval", 60.0))

        async with browser_context(
            "wechat", self.config.browser_state_dir, headless=headless,
            cdp_url=self.settings.option("cdp_url", ""),
        ) as context:
            if not await has_login(context, "wechat"):
                raise SourceError(
                    "微信公众号后台未登录。请先执行：\n"
                    "    python run.py login --platform wechat\n"
                    "然后扫码登录（用公众号管理员微信）。"
                )

            page = await context.new_page()
            await page.goto(f"{MP_BASE}/cgi-bin/home", wait_until="domcontentloaded")
            await asyncio.sleep(2)

            token = self._extract_token(page.url)
            if not token:
                # 有些账号登录后落在别的页面，从 cookie 里兜一次
                token = await self._token_from_cookie(context)
            if not token:
                raise SourceError(
                    "拿不到公众平台的 token，登录态可能已失效。"
                    "重新执行 python run.py login --platform wechat"
                )

            if kind == "keyword":
                # 后台没有全局关键词搜索，只能先搜公众号名再抓其文章
                accounts = [target]
            else:
                accounts = [target]

            all_posts: list[Post] = []
            for account in accounts:
                fakeid = await self._search_biz(page, token, account)
                if not fakeid:
                    continue
                await asyncio.sleep(min_interval)
                posts = await self._list_appmsg(page, token, fakeid, account, limit)
                all_posts.extend(posts)
                await asyncio.sleep(min_interval)

            await page.close()
            return all_posts[:limit]

    @staticmethod
    def _extract_token(url: str) -> str | None:
        m = re.search(r"[?&]token=(\d+)", url)
        return m.group(1) if m else None

    async def _token_from_cookie(self, context: Any) -> str | None:
        cookies = await context.cookies()
        for cookie in cookies:
            if cookie.get("name") == "token" and cookie.get("value", "").isdigit():
                return cookie["value"]
        return None

    async def _mp_get(self, page: Any, url: str) -> dict[str, Any]:
        """在页面上下文里发请求，复用登录态和 UA，并统一处理限流。"""
        result = await page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: 'include'});
                const text = await r.text();
                return {status: r.status, text};
            }""",
            url,
        )
        text = result.get("text", "")
        if any(marker in text for marker in MP_FREQ_MARKERS):
            raise SourceError(
                "公众平台返回「频率过快」。这是账号级封禁，通常需要等约 1 小时。"
                "本工具已按限流停止，请稍后再跑（建议一天 1~2 次增量同步）。"
            )
        import json

        try:
            return json.loads(text)
        except ValueError as exc:
            if '"base_resp"' in text:
                raise SourceError(f"公众平台返回无法解析的响应：{text[:200]}") from exc
            return {}

    async def _search_biz(self, page: Any, token: str, name: str) -> str | None:
        """按名字搜公众号，拿 fakeid。"""
        url = (
            f"{MP_BASE}/cgi-bin/searchbiz?action=search_biz&begin=0&count=5"
            f"&query={quote(name)}&token={token}&lang=zh_CN&f=json&ajax=1"
        )
        data = await self._mp_get(page, url)
        for item in (data.get("list") or []):
            if item.get("nickname") == name:
                return item.get("fakeid")
        first = (data.get("list") or [{}])[0]
        return first.get("fakeid")

    async def _list_appmsg(
        self, page: Any, token: str, fakeid: str, account: str, limit: int
    ) -> list[Post]:
        """拉某个公众号的历史文章列表。

        每页 5 条是后台的常规上限；为了控制请求数，默认只翻
        ceil(limit/5) 页，并且页间强制 sleep。
        """
        posts: list[Post] = []
        pages = max(1, (limit + 4) // 5)
        interval = float(self.settings.option("mp_min_interval", 60.0))

        for page_index in range(pages):
            url = (
                f"{MP_BASE}/cgi-bin/appmsg?action=list_ex&begin={page_index * 5}"
                f"&count=5&fakeid={fakeid}&type=9&query=&token={token}"
                f"&lang=zh_CN&f=json&ajax=1"
            )
            data = await self._mp_get(page, url)
            items = data.get("app_msg_list") or []
            if not items:
                break

            for item in items:
                url_ = item.get("link") or ""
                posts.append(
                    Post(
                        platform="wechat",
                        post_id=str(item.get("aid") or item.get("mid") or url_[:64]),
                        title=str(item.get("title") or ""),
                        content=str(item.get("digest") or ""),
                        author=account,
                        author_id=fakeid,
                        url=url_,
                        publish_time=parse_timestamp(item.get("create_time")),
                        views=to_int(item.get("read_num")),
                        likes=to_int(item.get("like_num")),
                        matched_by=account,
                        raw={"mode": "mp", "fakeid": fakeid},
                    )
                )

            if len(posts) >= limit:
                break
            await asyncio.sleep(interval)

        return posts

    # ------------------------------------------------------------------ 入口

    async def crawl_target(self, target: str, kind: str, limit: int) -> list[Post]:
        if self.mode == "sogou":
            if kind == "account":
                # 搜狗 type=1 是按公众号名搜号，type=2 是搜文章。
                # 对账号监控我们退化成「按号名搜文章」，效果接近。
                return await self._crawl_sogou(target, limit)
            return await self._crawl_sogou(target, limit)
        return await self._crawl_mp(target, kind, limit)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
