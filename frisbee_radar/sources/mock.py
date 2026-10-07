"""离线样例数据源。

用途：不联网、不登录就能把「采集 → 打分 → 去重 → 入库 → 出报告」
整条链路跑通，方便调关键词权重、改报告模板、写测试。

数据里故意混了两类噪音，用来验证相关性过滤是否生效：
  - 硬盘语境里的「飞盘」（负向词）
  - 只有泛户外词、没有飞盘核心词的内容（相关词不该单独计分）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Post
from .base import BaseSource

NOW = datetime.now(timezone.utc)


def _ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


# (平台, 标题, 正文, 作者, 互动量, 距今小时, url)
SAMPLE: list[dict[str, Any]] = [
    {
        "platform": "wechat",
        "title": "2025中国飞盘联赛总决赛收官，上海队首夺冠军",
        "content": "历时三个月的中国飞盘联赛在杭州落幕。决赛中上海飞盘队以 15:12 击败北京队，"
        "首次捧起冠军奖杯。本届联赛共有 24 支飞盘俱乐部参赛，创下历史新高。",
        "author": "中国飞盘联赛",
        "likes": 1284, "comments": 96, "shares": 312, "views": 45210,
        "hours_ago": 6,
        "url": "https://mp.weixin.qq.com/s/sample-001",
    },
    {
        "platform": "wechat",
        "title": "新手入门飞盘要买什么盘？一文说清 175g 与 145g 的区别",
        "content": "刚接触极限飞盘的朋友最常问的就是买什么盘。比赛用盘标准是 175g，"
        "而青少年和室内场常用 145g。本文还讲了如何判断飞盘的重心和手感。",
        "author": "飞盘世界",
        "likes": 866, "comments": 124, "shares": 201, "views": 20330,
        "hours_ago": 30,
        "url": "https://mp.weixin.qq.com/s/sample-002",
    },
    {
        "platform": "wechat",
        "title": "全国飞盘运动推广委员会发布 2025 年飞盘教练员培训计划",
        "content": "为规范飞盘教学市场，委员会将在全国 12 个城市开展飞盘教练员等级培训，"
        "首期报名即日开启。培训内容涵盖掷准飞盘与极限飞盘两套体系。",
        "author": "全国飞盘运动推广委员会",
        "likes": 432, "comments": 38, "shares": 155, "views": 12870,
        "hours_ago": 72,
        "url": "https://mp.weixin.qq.com/s/sample-003",
    },
    {
        "platform": "xiaohongshu",
        "title": "飞盘约局｜周末滨江公园新手友好局",
        "content": "每周六下午 3 点，滨江公园大草坪，新手友好，有教练带。自带盘或现场租都可以，"
        "评论区扣 1 拉群。#飞盘 #极限飞盘 #飞盘约局",
        "author": "杭州飞盘俱乐部",
        "likes": 523, "comments": 187, "collects": 96, "shares": 42,
        "hours_ago": 12,
        "url": "https://www.xiaohongshu.com/explore/mock-xhs-001",
    },
    {
        "platform": "xiaohongshu",
        "title": "飞盘穿搭分享｜运动也要好看",
        "content": "今天这套是速干短袖 + 运动短裙，飞盘场上跑动完全不受限。"
        "配色选了莫兰迪绿，拍照很出片 #飞盘 #飞盘穿搭 #运动穿搭",
        "author": "小鹿的运动日记",
        "likes": 2140, "comments": 233, "collects": 810, "shares": 122,
        "hours_ago": 40,
        "url": "https://www.xiaohongshu.com/explore/mock-xhs-002",
    },
    {
        "platform": "xiaohongshu",
        "title": "第一次玩飞盘就上手了！三个接盘小技巧",
        "content": "1. 双手夹盘要像拍手一样；2. 眼睛看盘不看人；3. 跑位要积极。"
        "练了两次就能接住了，飞盘真的比想象中容易上手 #飞盘入门 #极限飞盘",
        "author": "阿May爱运动",
        "likes": 1876, "comments": 302, "collects": 654, "shares": 88,
        "hours_ago": 96,
        "url": "https://www.xiaohongshu.com/explore/mock-xhs-003",
    },
    {
        "platform": "douyin",
        "title": "这记长传直接跨了半个场地！#飞盘 #极限飞盘",
        "content": "飞盘比赛现场，一记 40 米长传直接送到得分区，队友接盘得分。"
        "这个盘感太顶了 #飞盘 #极限飞盘 #飞盘联赛",
        "author": "飞盘解说老王",
        "likes": 45200, "comments": 1820, "shares": 3400, "collects": 2100,
        "hours_ago": 20,
        "url": "https://www.douyin.com/video/mock-dy-001",
    },
    {
        "platform": "douyin",
        "title": "飞盘教学：反手撇盘的三个常见错误",
        "content": "很多人反手盘出不去，是因为手腕没锁住。今天拆解三个常见错误动作，"
        "照着练一周就能改善 #飞盘教学 #极限飞盘",
        "author": "飞盘教练老张",
        "likes": 12800, "comments": 640, "shares": 900, "collects": 3100,
        "hours_ago": 55,
        "url": "https://www.douyin.com/video/mock-dy-002",
    },
    # ---------------- 以下为噪音，用于验证过滤 ----------------
    {
        "platform": "wechat",
        "title": "移动硬盘选购指南：这几款 SSD 比机械硬盘快得多",
        "content": "很多人口中的「飞盘」其实是硬盘的误读。本文讨论 1TB 移动硬盘盒与 "
        "SSD 的实际读写速度差异，以及磁盘阵列的搭建方式。",
        "author": "数码评测室",
        "likes": 320, "comments": 45, "shares": 20, "views": 8900,
        "hours_ago": 15,
        "url": "https://mp.weixin.qq.com/s/sample-noise-001",
    },
    {
        "platform": "xiaohongshu",
        "title": "周末露营 + 桨板 + 匹克球，一天玩遍三个项目",
        "content": "现在的户外运动越来越卷了，露营、桨板、匹克球、陆冲，"
        "一天安排得满满当当 #露营 #桨板 #匹克球",
        "author": "户外玩家小林",
        "likes": 980, "comments": 76, "collects": 220, "shares": 30,
        "hours_ago": 8,
        "url": "https://www.xiaohongshu.com/explore/mock-xhs-noise-001",
    },
    {
        "platform": "douyin",
        "title": "极限运动合集，看完肾上腺素飙升",
        "content": "翼装飞行、速降、攀岩、跑酷，这些极限运动你敢试哪个？"
        "#极限运动 #跑酷 #攀岩",
        "author": "极限运动精选",
        "likes": 23000, "comments": 1200, "shares": 2000, "collects": 800,
        "hours_ago": 33,
        "url": "https://www.douyin.com/video/mock-dy-noise-001",
    },
]


class MockSource(BaseSource):
    """按平台切分样例数据，模拟真实采集器的输出接口。"""

    platform = "mock"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 不指定目标时默认模拟公众号
        if not self.settings.keywords and not self.settings.accounts:
            self.settings.keywords = ["飞盘"]

    async def crawl_target(self, target: str, kind: str, limit: int) -> list[Post]:
        platform = self.settings.option("platform", "wechat")
        posts: list[Post] = []
        for row in SAMPLE:
            if row["platform"] != platform:
                continue
            posts.append(
                Post(
                    platform=row["platform"],
                    post_id=row["url"].rstrip("/").split("/")[-1],
                    title=row["title"],
                    content=row["content"],
                    author=row["author"],
                    url=row["url"],
                    publish_time=_ago(row["hours_ago"] / 24),
                    likes=row.get("likes", 0),
                    comments=row.get("comments", 0),
                    shares=row.get("shares", 0),
                    collects=row.get("collects", 0),
                    views=row.get("views", 0),
                    matched_by=target,
                    raw={"fixture": True},
                )
            )
        return posts[:limit]

    async def crawl(self, only: str | None = None):  # type: ignore[override]
        """样例源一次返回全部，不按目标重复产出——否则同一批数据会被
        当成不同关键词命中的结果反复入库。"""
        yield "飞盘", "keyword", await self.crawl_target("飞盘", "keyword", 999)
