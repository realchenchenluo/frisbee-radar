"""小红书采集。

做法：用持久化登录态的浏览器打开搜索页，监听页面自己发出的
`/api/sns/web/v1/search/notes` 响应。签名参数（x-s / x-t）由页面 JS
自己计算并带上，我们不需要逆向它——这正是 MediaCrawler 的核心思路，
平台改签名算法时本模块不用改。

需要登录的原因：小红书未登录状态下搜索接口只返回极少内容，且很快
要求登录。登录一次后长期有效（cookie 有效期通常数周到数月）。

已知限制：
  - 搜索结果的正文是摘要级的（display_title + desc），笔记正文需要
    逐条打开笔记页，成本高，默认不做（可用 --fetch-detail 开启）。
  - 翻页靠滚动触发，页面上限由平台控制，通常 10~20 页。
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from ..browser import browser_context, goto_and_collect
from ..models import Post
from .base import BaseSource, SourceError, parse_timestamp, to_int

# 搜索接口。⚠️ 这里是实测对齐过的，别凭印象改：
#   · 域名是 so.xiaohongshu.com，不是 edith.xiaohongshu.com
#     （edith 那个域负责 config / user-me / onebox / filter 等，
#      真正返回笔记列表的搜索接口在 so 域上）
#   · 版本是 **v2**，不是 v1
# 一开始写的是 /api/sns/web/v1/search/notes，结果采集一条都拿不到 ——
# 接口根本没触发。用 run.py probe 抓真实响应才定位到。
# 响应结构：{data: {items: [{id, model_type:"note", note_card: {...}, xsec_token}],
#                  has_more}}
SEARCH_API = "/api/sns/web/v2/search/notes"

# 账号主页的作品列表接口（这条是照早期资料写的，尚未在真实登录态下验证过；
# 如果账号监控拿不到内容，用 probe 打开主页对齐一次）
USER_POSTED_API = "/api/sns/web/v1/user_posted"

# 排序方式，对应页面上的「综合 / 最新 / 最热」
SORT_PARAM = {
    "general": "general",
    "time_descending": "time_descending",
    "popularity_descending": "popularity_descending",
}


class XiaohongshuSource(BaseSource):
    platform = "xiaohongshu"

    # 关于登录判定：这里**故意不做采集前的拦截**。
    #
    # 实测（2026-10-06）小红书的登录态判断没有可靠信号：
    #   · 自动化 Chromium 匿名访问 /explore → 302 到 /login，但这时 cookie 里
    #     已经有 web_session 了（所以看 cookie 会误判成已登录）
    #   · 真实 Chrome 匿名访问 /explore → 不跳登录页，cookie 里反而没有 web_session
    #   · __INITIAL_STATE__ 是 null，DOM 上「登录/发布/我」匿名都有
    #   · 直接 fetch 搜索接口拿不到有效响应（需要页面自己算的 x-s 签名）
    #
    # 判错了的代价不对称：误判成「未登录」会把已经登录好的用户挡在外面，
    # 而且他会反复扫码都进不去。所以改成采完再看结果 —— 一条都没拿到时
    # 列出可能原因，用户在报错里就能看到「未登录」这一条。

    async def crawl_target(self, target: str, kind: str, limit: int) -> list[Post]:
        headless = bool(self.settings.option("headless", False))
        async with browser_context(
            "xiaohongshu", self.config.browser_state_dir, headless=headless,
            cdp_url=self.settings.option("cdp_url", ""),
        ) as context:
            if kind == "account":
                posts = await self._crawl_account(target, limit, context)
            else:
                posts = await self._crawl_keyword(target, limit, context)

        if not posts:
            raise SourceError(self._empty_result_hint(target))
        return posts

    @staticmethod
    def _empty_result_hint(target: str) -> str:
        return (
            f"小红书「{target}」没有拿到任何内容。可能的原因，按可能性排序：\n"
            "  1. 未登录 / 登录态过期 —— 最常见。两种解决方式：\n"
            "       · 双击「3-登录小红书.bat」扫码\n"
            "       · 或双击「7-用我的浏览器.bat」，直接复用你自己浏览器里\n"
            "         已经登录好的小红书（不用扫码）\n"
            "  2. 这个词在小红书上确实没有结果\n"
            "  3. 平台改版，搜索接口变了 —— 改 sources/xiaohongshu.py 里的 SEARCH_API\n"
            "     用 python run.py probe --platform xiaohongshu --keyword 飞盘 看实际接口"
        )

    @staticmethod
    def _extract_search_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """从搜索响应里取出笔记卡片。

        结构随版本有差异，这里兼容 data.items / data.data.items 两种。
        """
        data = payload.get("data")
        if isinstance(data, dict):
            items = data.get("items") or data.get("data") or []
        elif isinstance(data, list):
            items = data
        else:
            return []

        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            card = item.get("note_card") or item.get("noteCard")
            if not card:
                continue
            out.append({"id": item.get("id") or card.get("note_id"), "card": card,
                        "xsec_token": item.get("xsec_token") or card.get("xsec_token", "")})
        return out

    @staticmethod
    def _extract_user_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data") or {}
        notes = data.get("notes") or data.get("items") or []
        out = []
        for note in notes:
            if not isinstance(note, dict):
                continue
            out.append({"id": note.get("note_id") or note.get("id"), "card": note,
                        "xsec_token": note.get("xsec_token", "")})
        return out

    @staticmethod
    def _parse_corner_time(card: dict[str, Any], now: Any = None) -> Any:
        """从 corner_tag_info 里挖发布时间。

        搜索接口**不给时间戳**，只在 corner_tag_info 里塞一个标签：
            [{"type": "publish_time", "text": "09-10"}]
        而且只有月日、没有年份。年份只能推断：
          按今年算，如果算出来的日期比"现在"还晚，那它必然是去年发的
          （笔记不可能发布于未来）。

        副作用的取舍：跨年那几天，一条 1 月初发的笔记可能被算成去年。
        对"按时间排序"这个用途来说，最多错几天，比完全没有时间好得多。
        """
        import re
        from datetime import datetime, timedelta, timezone

        tags = card.get("corner_tag_info")
        if not isinstance(tags, list):
            return None

        for tag in tags:
            if not isinstance(tag, dict) or tag.get("type") != "publish_time":
                continue
            text = str(tag.get("text") or "").strip()
            m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", text)
            if not m:
                continue

            month, day = int(m.group(1)), int(m.group(2))
            tz = timezone(timedelta(hours=8))   # 小红书是国内平台，按北京时间推
            current = now or datetime.now(tz)
            try:
                candidate = datetime(current.year, month, day, tzinfo=tz)
            except ValueError:
                return None      # 比如 2-29 遇到平年
            if candidate > current:
                try:
                    candidate = candidate.replace(year=current.year - 1)
                except ValueError:
                    return None
            return candidate

        return None

    def _to_post(self, entry: dict[str, Any], matched_by: str) -> Post | None:
        card = entry.get("card") or {}
        note_id = entry.get("id") or card.get("note_id")
        if not note_id:
            return None

        user = card.get("user") or {}
        interact = card.get("interact_info") or card.get("interactInfo") or {}
        cover = card.get("cover") or {}
        note_type = card.get("type") or "normal"

        images: list[str] = []
        for key in ("url_default", "url_pre", "url"):
            if cover.get(key):
                images.append(cover[key])
                break

        # 笔记地址必须带 xsec_token，否则打开会被拦
        token = entry.get("xsec_token") or ""
        url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if token:
            url += f"?xsec_token={token}&xsec_source=pc_search"

        return Post(
            platform="xiaohongshu",
            post_id=str(note_id),
            title=str(card.get("display_title") or ""),
            content=str(card.get("desc") or ""),
            author=str(user.get("nickname") or user.get("nick_name") or ""),
            author_id=str(user.get("user_id") or user.get("userId") or ""),
            url=url,
            publish_time=(
                parse_timestamp(card.get("time") or card.get("publish_time"))
                or self._parse_corner_time(card)
            ),
            likes=to_int(interact.get("liked_count") or interact.get("likedCount")),
            comments=to_int(interact.get("comment_count") or interact.get("commentCount")),
            collects=to_int(interact.get("collected_count") or interact.get("collectedCount")),
            shares=to_int(interact.get("shared_count") or interact.get("sharedCount")),
            images=images,
            matched_by=matched_by,
            raw={"note_type": note_type, "xsec_token": token},
        )

    async def _crawl_keyword(self, keyword: str, limit: int, context: Any) -> list[Post]:
        sort = SORT_PARAM.get(str(self.settings.option("sort", "general")), "general")
        url = (
            "https://www.xiaohongshu.com/search_result"
            f"?keyword={quote(keyword)}&source=web_explore_feed&type=51&sort={sort}"
        )

        entries = await goto_and_collect(
            context,
            url,
            SEARCH_API,
            self._extract_search_items,
            max_scrolls=int(self.settings.option("max_scrolls", 10)),
            limit=limit,
        )

        posts: list[Post] = []
        for entry in entries:
            post = self._to_post(entry, keyword)
            if post:
                posts.append(post)
        return posts

    async def _crawl_account(self, account: str, limit: int, context: Any) -> list[Post]:
        """账号监控。account 可以填 user_id，也可以是主页链接。"""
        user_id = account.rstrip("/").split("/")[-1].split("?")[0]
        url = f"https://www.xiaohongshu.com/user/profile/{user_id}"

        entries = await goto_and_collect(
            context,
            url,
            USER_POSTED_API,
            self._extract_user_items,
            max_scrolls=int(self.settings.option("max_scrolls", 8)),
            limit=limit,
        )

        posts: list[Post] = []
        for entry in entries:
            post = self._to_post(entry, account)
            if post:
                post.author_id = post.author_id or user_id
                if not post.author:
                    post.author = account
                posts.append(post)
        return posts
