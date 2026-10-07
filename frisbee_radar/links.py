"""链接可用性检查。

## 为什么需要这个

微信公众号的链接是**会过期的**：搜狗给的跳转地址形如
`mp.weixin.qq.com/s?src=11&timestamp=..&signature=..`，签名有时间窗。
实测采后约 4 小时还能打开，两天后就不是文章页了。网站上的卡片如果
不标注、不检查，读者点进去就是一片空白。

## 各平台的判据（实测得出，别凭印象改）

**公众号 —— 能便宜地查**
    失效页 HTTP 200 但只有 **27,794 字节**（固定的错误页模板），
    有效文章是 **3.5 MB** 上下，差 126 倍。用 HTTP 请求就能判定，
    不用开浏览器。阈值取 100 KB：离失效侧有 3.6 倍余量，离有效侧 35 倍。

**小红书 —— 查不了，也不该查**
    普通 HTTP 请求**一律被重定向到登录页**，所以所有链接看起来都"失效"，
    这个判据完全不能用。要准确判定必须带登录态开浏览器，而小红书链接
    本身就没有发现过期现象（实测几小时后仍能打开），
    花几十次页面加载去验一个不会坏的东西不划算。
    所以小红书标记为 unknown，卡片上如实说明"需要登录小红书才能看"。

## 成本控制

结果按链接缓存（`link_status` + `link_checked_ts`），
只有超过 `recheck_hours` 才会重新检查，不重复打平台。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .models import Post

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 公众号有效文章的页面大小在 3MB 量级，失效页固定约 27KB。
# 取 100KB 作阈值：到失效侧余量 3.6 倍，到有效侧 35 倍。
WECHAT_ALIVE_MIN_BYTES = 100_000

# 链接状态
STATUS_OK = "ok"
STATUS_DEAD = "dead"
STATUS_UNKNOWN = "unknown"


@dataclass
class LinkCheck:
    platform: str
    post_id: str
    status: str
    detail: str = ""


async def _check_wechat(
    client: httpx.AsyncClient, posts: list[Post], concurrency: int = 5
) -> list[LinkCheck]:
    """公众号：HTTP 请求 + 页面大小判定。

    并发控制在 5 —— 这是在对微信服务器发请求，别开太大。
    """
    sem = asyncio.Semaphore(concurrency)
    results: list[LinkCheck] = []

    async def one(post: Post) -> None:
        async with sem:
            try:
                resp = await client.get(post.url)
                size = len(resp.content)
                if size >= WECHAT_ALIVE_MIN_BYTES:
                    results.append(LinkCheck(post.platform, post.post_id, STATUS_OK,
                                             f"{size // 1024}KB"))
                else:
                    results.append(LinkCheck(post.platform, post.post_id, STATUS_DEAD,
                                             f"只有 {size // 1024}KB，不是文章页"))
            except Exception as exc:
                # 网络问题不等于链接失效 —— 标 unknown，下次再查，
                # 免得把好链接误判成坏的
                results.append(LinkCheck(post.platform, post.post_id, STATUS_UNKNOWN,
                                         type(exc).__name__))
            await asyncio.sleep(0.3)

    await asyncio.gather(*(one(p) for p in posts))
    return results


async def check_links(
    posts: list[Post],
    *,
    timeout: float = 20.0,
    concurrency: int = 5,
) -> list[LinkCheck]:
    """检查一批链接。只处理能判定的平台（目前只有公众号）。"""
    wechat = [p for p in posts if p.platform == "wechat" and p.url]
    if not wechat:
        return []

    async with httpx.AsyncClient(
        headers={"User-Agent": UA}, timeout=timeout, follow_redirects=True
    ) as client:
        return await _check_wechat(client, wechat, concurrency=concurrency)


def needs_recheck(last_checked: datetime | None, hours: int = 24) -> bool:
    """这个链接该不该重新查。

    刚采到的链接是有效的，没必要立刻查一遍；查得太勤是在白打平台。
    """
    if last_checked is None:
        return True
    if last_checked.tzinfo is None:
        last_checked = last_checked.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_checked >= timedelta(hours=hours)
