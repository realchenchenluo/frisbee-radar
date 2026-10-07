"""抖音采集。

同小红书：跑一个带登录态的浏览器，监听搜索接口
`/aweme/v1/web/general/search/single/` 的响应。a_bogus 签名由页面
自己的 JS 生成，不需要逆向。

需要登录的原因：不登录时抖音搜索页只给很少结果，且会频繁弹验证码。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..browser import browser_context, goto_and_collect
from ..models import Post
from .base import BaseSource, SourceError, parse_timestamp, to_int

SEARCH_API = "/aweme/v1/web/general/search/single/"
POST_API = "/aweme/v1/web/aweme/post/"


class DouyinSource(BaseSource):
    platform = "douyin"

    # 抖音的 sessionid 系列 cookie 实测确实是登录后才有的（匿名访客只有
    # ttwid/odin_tt/__ac_nonce），判据本身没错。但为了和小红书保持一致、
    # 也避免将来它变成匿名就种导致误判，这里同样不做采集前拦截 ——
    # 采完一条都没有时再列出可能原因。

    async def crawl_target(self, target: str, kind: str, limit: int) -> list[Post]:
        headless = bool(self.settings.option("headless", False))
        async with browser_context(
            "douyin", self.config.browser_state_dir, headless=headless,
            cdp_url=self.settings.option("cdp_url", ""),
        ) as context:
            if kind == "account":
                posts = await self._crawl_account(target, limit, context)
            else:
                posts = await self._crawl_keyword(target, limit, context)

        if not posts:
            raise SourceError(
                f"抖音「{target}」没有拿到任何内容。可能的原因：\n"
                "  1. 未登录 / 登录态过期 —— 最常见。两种解决方式：\n"
                "       · 双击「4-登录抖音.bat」扫码\n"
                "       · 或双击「7-用我的浏览器.bat」，复用你自己浏览器里\n"
                "         已经登录好的抖音（不用扫码）\n"
                "  2. 抖音弹了验证码/滑块 —— 手动开一次浏览器过掉\n"
                f"  3. 这个词确实没有结果\n"
                "  4. 平台改版，接口变了 —— 改 sources/douyin.py 里的 SEARCH_API"
            )
        return posts

    @staticmethod
    def _extract_search_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """从搜索响应里取视频。

        data 是列表，元素形如 {"type": 1, "aweme_info": {...}}；
        type=1 是视频，其余是直播/用户/话题等，直接跳过。
        """
        items = payload.get("data")
        if not isinstance(items, list):
            return []

        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            info = item.get("aweme_info") or item.get("aweme_mix_info", {}).get("mix_items")
            if isinstance(info, list):
                info = info[0] if info else None
            if isinstance(info, dict) and info.get("aweme_id"):
                out.append(info)
        return out

    @staticmethod
    def _extract_post_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        items = payload.get("aweme_list") or []
        return [i for i in items if isinstance(i, dict) and i.get("aweme_id")]

    def _to_post(self, aweme: dict[str, Any], matched_by: str) -> Post | None:
        aweme_id = aweme.get("aweme_id")
        if not aweme_id:
            return None

        author = aweme.get("author") or {}
        stats = aweme.get("statistics") or {}
        video = aweme.get("video") or {}

        cover = ""
        cov = video.get("cover") or video.get("origin_cover") or {}
        covers = cov.get("url_list") or []
        if covers:
            cover = covers[0]

        images = [cover] if cover else []

        return Post(
            platform="douyin",
            post_id=str(aweme_id),
            title=str(aweme.get("desc") or "")[:120],
            content=str(aweme.get("desc") or ""),
            author=str(author.get("nickname") or ""),
            author_id=str(author.get("sec_uid") or author.get("uid") or ""),
            url=f"https://www.douyin.com/video/{aweme_id}",
            publish_time=parse_timestamp(aweme.get("create_time")),
            likes=to_int(stats.get("digg_count")),
            comments=to_int(stats.get("comment_count")),
            shares=to_int(stats.get("share_count")),
            collects=to_int(stats.get("collect_count")),
            views=to_int(stats.get("play_count")),
            images=images,
            videos=list((video.get("play_addr") or {}).get("url_list") or [])[:1],
            matched_by=matched_by,
            raw={
                "duration": aweme.get("duration"),
                "is_top": aweme.get("is_top"),
            },
        )

    async def _crawl_keyword(self, keyword: str, limit: int, context: Any) -> list[Post]:
        sort = int(self.settings.option("sort_type", 0) or 0)
        url = f"https://www.douyin.com/search/{quote(keyword)}?type=video&sort_type={sort}"

        entries = await goto_and_collect(
            context,
            url,
            SEARCH_API,
            self._extract_search_items,
            max_scrolls=int(self.settings.option("max_scrolls", 10)),
            limit=limit,
        )

        posts: list[Post] = []
        for aweme in entries:
            post = self._to_post(aweme, keyword)
            if post:
                posts.append(post)
        return posts

    async def _crawl_account(self, account: str, limit: int, context: Any) -> list[Post]:
        """账号监控。account 填 sec_uid 或主页链接。"""
        sec_uid = account.rstrip("/").split("?")[0].split("/")[-1]
        url = f"https://www.douyin.com/user/{sec_uid}"

        entries = await goto_and_collect(
            context,
            url,
            POST_API,
            self._extract_post_items,
            max_scrolls=int(self.settings.option("max_scrolls", 8)),
            limit=limit,
        )

        posts: list[Post] = []
        for aweme in entries:
            post = self._to_post(aweme, account)
            if post:
                post.author_id = post.author_id or sec_uid
                posts.append(post)
        return posts
