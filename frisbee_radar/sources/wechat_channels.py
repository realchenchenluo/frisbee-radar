"""微信视频号（微信小视频）采集 —— 目前**做不到**，原因见下。

## 实测结论（2026-10-06）

**视频号在网页端没有可以按关键词搜内容的入口。** 三组证据：

1. `channels.weixin.qq.com` 根路径 → 302 到 `/login.html`，页面标题是
   **「视频号助手」**，顶部导航只有：视频号助手 / 加热平台 / 机构管理 /
   特效平台 / 微信小店 / 联盟带货机构 —— 全是创作者和商家后台，没有
   面向读者的内容浏览或搜索。
2. `/web/pages/home`（我一开始猜的"网页版首页"）是**空页面**：
   截图 6.8KB、body 文本长度 0、canvas/img/iframe 全为 0。
   也就是说它既不是浏览页也不是登录页，纯白。
3. GitHub 上的视频号项目只有两类：挂微信 PC 客户端的**下载器**
   （`ltaoo/wx_channels_download` 9.6k、`qiye45/wechatVideoDownload` 5.8k，
   做的是抓视频文件，不是元数据检索），或者走**付费第三方 API**
   （`wechat-video-collect` 里是 `axios.post(config.apiUrl)`，config 不进仓库）。
   没有 MediaCrawler 那种可参考的元数据采集实现。

## 还剩下什么可能

唯一的官方 Web 入口是「视频号助手」，而它要求你**名下有一个视频号**才能登录。
登录进去之后能看到的是**你自己账号**的内容管理，不是全平台检索。
它内部有没有内容发现/搜索类的接口，只有登录后 probe 才知道 ——
`python run.py probe --platform wechat_channels --keyword 飞盘` 会把
真实请求到的接口和响应落盘。

## 这份代码为什么还留着

下面的提取逻辑（`_find_cards` / `_to_post`）是有价值的：它对响应结构做
通用遍历、优先认 `objectDesc` 包装层，只要哪天视频号开了网页端搜索、
或者你从别的渠道拿到了视频号 JSON，这套提取就能直接用。离线测试覆盖了它。

但**在确认有可用接口之前，不要打开 `enabled`** —— `crawl_target` 会直接
报错说明原因，而不是假装搜了一圈然后告诉你"没找到内容"。
"""

from __future__ import annotations

from typing import Any

from ..models import Post
from .base import BaseSource, SourceError, parse_timestamp, to_int

# 视频号没有公开搜索页。这里保留曾经试过的路径只作为文档：
# /web/pages/search 和 /web/pages/home 实测都无法用来按关键词取内容。
NO_PUBLIC_SEARCH_PATHS = (
    "/web/pages/home",
    "/web/pages/search",
)

# 响应 URL 里出现这些片段就认为是候选内容接口（probe 命令用）
API_HINTS = ("search", "object", "feed", "finder", "recommend", "profile")

# 明确要跳过的（埋点、性能上报之类）
SKIP_HINTS = ("report", "perf", "beacon", "log", "metrics", "trace")

NOT_SUPPORTED_MESSAGE = (
    "视频号没有可以在网页端按关键词搜内容的入口，这个平台做不了采集。\n"
    "\n"
    "实测依据：\n"
    "  · channels.weixin.qq.com 根路径跳转到「视频号助手」——创作者后台，\n"
    "    只能管自己的号，没有面向读者的内容浏览/搜索\n"
    "  · 曾以为是网页版首页的 /web/pages/home 是空页面（截图 6.8KB、文本 0）\n"
    "  · GitHub 上的现成方案只有挂微信 PC 客户端的下载器（抓视频文件）\n"
    "    和付费第三方 API，没有可参考的元数据检索实现\n"
    "\n"
    "如果你名下有一个视频号，可以登录「视频号助手」后用下面的命令看看\n"
    "它内部暴露了哪些接口（也许有内容发现类的）：\n"
    "    python run.py login --platform wechat_channels\n"
    "    python run.py probe --platform wechat_channels --keyword 飞盘\n"
    "把 probe 的输出发我，能判断还有没有可用的抓法。"
)


class WechatChannelsSource(BaseSource):
    platform = "wechat_channels"

    # ------------------------------------------------------------------ 提取

    @staticmethod
    def _find_cards(node: Any, out: list[dict], depth: int = 0) -> None:
        """递归找「像视频卡片」的字典。

        优先认 objectDesc —— 视频号的数据结构里它是内容包装层
        （公开的第三方实现里出现过 item.objectDesc.topic.finderTopicInfo）。
        找不到就退化成启发式：同时有 id 类字段和标题类字段的字典。
        """
        if depth > 14 or len(out) > 400:
            return
        if isinstance(node, dict):
            if isinstance(node.get("objectDesc"), dict):
                out.append(node)
                return
            looks_like_card = (
                any(k in node for k in ("id", "objectId", "object_id", "exportId", "feedId"))
                and any(k in node for k in ("title", "desc", "description", "content"))
                and not any(k in node for k in ("errMsg", "errmsg", "base_resp"))
            )
            if looks_like_card:
                out.append(node)
                return
            for value in node.values():
                WechatChannelsSource._find_cards(value, out, depth + 1)
        elif isinstance(node, list):
            for value in node:
                WechatChannelsSource._find_cards(value, out, depth + 1)

    @staticmethod
    def _first(node: dict, *keys: str) -> Any:
        for key in keys:
            value = node.get(key)
            if value not in (None, "", [], {}):
                return value
        return None

    def _to_post(self, card: dict, matched_by: str) -> Post | None:
        # objectDesc 是外层包装，真正的字段大多在里面
        desc = card.get("objectDesc") if isinstance(card.get("objectDesc"), dict) else card
        desc = desc or {}

        title = self._first(desc, "title")
        body = self._first(desc, "description", "desc", "content", "abstract")
        if not title and not body:
            return None
        if not title:
            title = str(body)[:80]

        # 作者的字段层级在各版本里不一样，逐层找
        author = ""
        for holder in (
            desc.get("contact"),
            desc.get("authorInfo"),
            desc.get("author"),
            card.get("contact"),
            card.get("authorInfo"),
        ):
            if isinstance(holder, dict):
                author = str(
                    WechatChannelsSource._first(
                        holder, "nickname", "nickName", "nick_name", "name", "username"
                    ) or ""
                )
                if author:
                    break

        author_id = ""
        for holder in (desc.get("contact"), card.get("contact")):
            if isinstance(holder, dict):
                author_id = str(self._first(holder, "username", "id", "finderUsername") or "")
                if author_id:
                    break

        # 时间：视频号用秒级时间戳
        publish = parse_timestamp(
            self._first(desc, "createTime", "createtime", "create_time", "publishTime")
        )

        stats = {}
        for holder in (desc.get("likeInfo"), desc.get("statistics"), desc.get("stats")):
            if isinstance(holder, dict):
                stats.update(holder)

        post_id = str(
            self._first(card, "id", "objectId", "object_id", "exportId", "feedId")
            or self._first(desc, "id", "objectId", "exportId")
            or ""
        )
        if not post_id:
            return None

        # 视频号没有稳定的公开播放页 URL，能拿到 exportId 就能拼一个
        export_id = self._first(card, "exportId", "objectId") or post_id
        url = f"https://channels.weixin.qq.com/web/pages/feed?eid={export_id}"

        return Post(
            platform=self.platform,
            post_id=post_id,
            title=str(title),
            content=str(body or ""),
            author=author,
            author_id=author_id,
            url=url,
            publish_time=publish,
            likes=to_int(self._first(stats, "likeCount", "like_count")),
            comments=to_int(self._first(stats, "commentCount", "comment_count")),
            shares=to_int(self._first(stats, "forwardCount", "shareCount")),
            collects=to_int(self._first(stats, "favCount", "collectCount")),
            matched_by=matched_by,
            raw={"source": "wechat_channels", "keys": sorted(card.keys())[:20]},
        )

    # ------------------------------------------------------------------ 采集

    # 这里原本有一个 _collect()：打开搜索页、按响应特征收 JSON、滚动加载。
    # 在确认视频号没有公开搜索页之后删掉了 —— 留着一段没人调用的死代码
    # 比没有更糟。真正可复用的是上面的提取逻辑（_find_cards / _to_post），
    # 它们不依赖任何具体 URL，probe 命令也直接在用。

    async def crawl_target(self, target: str, kind: str, limit: int) -> list[Post]:
        """直接说明做不到，而不是搜一圈再报「没找到内容」。

        假装搜索的坏处：用户会以为是关键词不对、或者登录态失效，
        于是反复排查一个根本不存在的问题。
        """
        raise SourceError(NOT_SUPPORTED_MESSAGE)
