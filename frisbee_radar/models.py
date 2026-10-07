"""统一数据模型。

三个平台的原始字段差异极大（抖音是 aweme/statistics，小红书是 note_card/interact_info，
公众号是 app_msg_list），全部在这一层归一化成 `Post`。下游的存储、打分、报告只认 `Post`，
新增平台时不需要动下游代码。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# 平台标识 -> 中文显示名
PLATFORM_LABELS: dict[str, str] = {
    "wechat": "微信公众号",
    "wechat_channels": "微信视频号",
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "mock": "离线样例",
}


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform)


def _to_ts(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _from_ts(ts: int | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@dataclass
class Post:
    """一条归一化后的内容。"""

    platform: str
    post_id: str
    title: str = ""
    content: str = ""
    author: str = ""
    author_id: str = ""
    url: str = ""
    publish_time: datetime | None = None

    likes: int = 0
    comments: int = 0
    shares: int = 0
    collects: int = 0
    views: int = 0

    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)

    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # 是通过哪个关键词/账号命中的，用于回溯
    matched_by: str = ""
    # 飞盘相关性 0~1，由 relevance.py 填充
    relevance: float = 0.0
    relevance_hits: list[str] = field(default_factory=list)
    # 链接可用性：'' 未查 / ok / dead / unknown，由 links.py 填充。
    # 公众号链接会过期，网页上要据此提示读者。
    link_status: str = ""
    link_checked_ts: int | None = None
    # 平台原始返回，排障用；报告里不输出
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """标题 + 正文，供相关性打分和去重用。"""
        return f"{self.title}\n{self.content}".strip()

    @property
    def content_hash(self) -> str:
        """正文指纹，用于跨 post_id 的重复识别。

        只取前 200 字并做空白折叠，避免平台在正文尾部拼接推荐位/
        「展开」之类的动态文案导致哈希抖动。
        """
        norm = "".join(self.text.split())[:200]
        return hashlib.sha1(norm.encode("utf-8")).hexdigest()

    @property
    def is_hash_distinctive(self) -> bool:
        """正文是否长到足以用作去重指纹。

        短视频平台上大量作品的文案就只有几个话题标签（「#飞盘 #极限飞盘」），
        这种短文本之间 hash 撞车是常态，拿它去重会把一堆不同视频当成同一条。
        低于阈值时只认 post_id。
        """
        return len("".join(self.text.split())) >= 40

    @property
    def engagement(self) -> int:
        """互动总量，用于报告排序。"""
        return self.likes + self.comments + self.shares + self.collects

    def is_valid(self) -> bool:
        """至少要能定位到一条内容才入库。"""
        return bool(self.post_id) and bool(self.text or self.url)

    def to_row(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "post_id": self.post_id,
            "title": self.title,
            "content": self.content,
            "author": self.author,
            "author_id": self.author_id,
            "url": self.url,
            "publish_ts": _to_ts(self.publish_time),
            "likes": self.likes,
            "comments": self.comments,
            "shares": self.shares,
            "collects": self.collects,
            "views": self.views,
            "images": json.dumps(self.images, ensure_ascii=False),
            "videos": json.dumps(self.videos, ensure_ascii=False),
            "collected_ts": _to_ts(self.collected_at),
            "matched_by": self.matched_by,
            "relevance": self.relevance,
            "relevance_hits": json.dumps(self.relevance_hits, ensure_ascii=False),
            "content_hash": self.content_hash,
            "link_status": self.link_status,
            "link_checked_ts": self.link_checked_ts,
            "raw": json.dumps(self.raw, ensure_ascii=False, default=str),
        }

    @classmethod
    def from_row(cls, row: Any) -> "Post":
        def _loads(value: Any, fallback: Any) -> Any:
            if not value:
                return fallback
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return fallback

        return cls(
            platform=row["platform"],
            post_id=row["post_id"],
            title=row["title"] or "",
            content=row["content"] or "",
            author=row["author"] or "",
            author_id=row["author_id"] or "",
            url=row["url"] or "",
            publish_time=_from_ts(row["publish_ts"]),
            likes=row["likes"] or 0,
            comments=row["comments"] or 0,
            shares=row["shares"] or 0,
            collects=row["collects"] or 0,
            views=row["views"] or 0,
            images=_loads(row["images"], []),
            videos=_loads(row["videos"], []),
            collected_at=_from_ts(row["collected_ts"]) or datetime.now(timezone.utc),
            matched_by=row["matched_by"] or "",
            relevance=row["relevance"] or 0.0,
            relevance_hits=_loads(row["relevance_hits"], []),
            link_status=(
                row["link_status"] if "link_status" in row.keys() else ""
            ) or "",
            link_checked_ts=(
                row["link_checked_ts"] if "link_checked_ts" in row.keys() else None
            ),
            raw=_loads(row["raw"], {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """给 JSON/JSONL 导出用的可序列化字典。"""
        row = self.to_row()
        row["publish_time"] = self.publish_time.isoformat() if self.publish_time else None
        row["collected_at"] = self.collected_at.isoformat()
        row.pop("publish_ts", None)
        row.pop("collected_ts", None)
        row.pop("content_hash", None)
        return row
