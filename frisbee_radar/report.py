"""报告生成。

把库里筛出来的飞盘内容整理成一份可以直接读的 Markdown 简报：
先给平台概览和高互动内容，再按平台分组列明细。
公众号/小红书/抖音的内容形态差别大，所以分组呈现而不是混排。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Post, platform_label

# 公众号按阅读量排，视频平台按点赞排，更贴近各自的「火」的定义
PRIMARY_METRIC = {
    "wechat": ("views", "阅读"),
    "xiaohongshu": ("likes", "点赞"),
    "douyin": ("likes", "点赞"),
}


def _metric(post: Post) -> tuple[int, str]:
    field, label = PRIMARY_METRIC.get(post.platform, ("likes", "点赞"))
    return getattr(post, field, 0) or post.likes, label


def _metric_text(post: Post) -> str:
    """互动数据文案。没有数据时返回空串，不要显示「阅读 0」。

    公众号搜狗模式拿不到阅读量（只有登录后台的 mp 模式才有），
    这种情况下宁可不显示，也不要让 0 误导读者以为内容没人看。
    """
    value, label = _metric(post)
    return f"{label} {_fmt_count(value)}" if value else ""


def _fmt_count(value: int) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "时间未知"
    local = dt.astimezone(timezone(timedelta(hours=8)))
    delta = datetime.now(timezone.utc) - dt
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)} 分钟前"
    if hours < 24:
        return f"{int(hours)} 小时前"
    if hours < 24 * 30:
        return f"{int(hours / 24)} 天前"
    return local.strftime("%Y-%m-%d")


def _clean(text: str, limit: int = 120) -> str:
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


class ReportBuilder:
    def __init__(self, posts: list[Post], title: str = "飞盘讯息简报"):
        # 报告只呈现飞盘相关的内容，噪音在入库时就已标 0 分
        self.posts = [p for p in posts if p.relevance > 0]
        self.title = title

    def _grouped(self) -> dict[str, list[Post]]:
        grouped: dict[str, list[Post]] = defaultdict(list)
        for post in self.posts:
            grouped[post.platform].append(post)
        for items in grouped.values():
            items.sort(
                key=lambda p: (p.relevance, _metric(p)[0]),
                reverse=True,
            )
        return grouped

    def to_markdown(self, top_n: int = 10) -> str:
        grouped = self._grouped()
        now = datetime.now(timezone(timedelta(hours=8)))

        lines: list[str] = []
        lines.append(f"# {self.title}")
        lines.append("")
        lines.append(f"生成时间：{now.strftime('%Y-%m-%d %H:%M')}（北京时间）　"
                     f"共 **{len(self.posts)}** 条飞盘相关讯息")
        lines.append("")

        if not self.posts:
            lines.append("> 本次没有筛出飞盘相关内容。可能是时间窗口内确实没有新讯息，"
                         "也可以试试放宽 `since_days` 或降低 `threshold`。")
            return "\n".join(lines) + "\n"

        # ---- 概览 ----
        lines.append("## 一、平台概览")
        lines.append("")
        lines.append("| 平台 | 条数 | 互动总计 | 相关内容最多的账号 |")
        lines.append("| --- | ---: | ---: | --- |")
        no_metric: list[str] = []
        for platform, items in sorted(
            grouped.items(), key=lambda kv: len(kv[1]), reverse=True
        ):
            total = sum(_metric(p)[0] for p in items)
            authors = Counter(p.author for p in items if p.author)
            top_author = authors.most_common(1)[0][0] if authors else "—"
            if total:
                total_text = _fmt_count(total)
            else:
                total_text = "—"
                no_metric.append(platform_label(platform))
            lines.append(
                f"| {platform_label(platform)} | {len(items)} | {total_text} | {top_author} |"
            )
        lines.append("")
        if no_metric:
            lines.append(
                f"> 注：{'、'.join(no_metric)} 没有拿到互动数据，"
                "该类内容按飞盘相关度排序。公众号要在 `mode: mp` 下登录后台才有阅读量。"
            )
            lines.append("")

        # ---- 高互动榜 ----
        ranked = sorted(self.posts, key=lambda p: _metric(p)[0], reverse=True)[:top_n]
        if ranked:
            lines.append(f"## 二、热度 Top {len(ranked)}")
            lines.append("")
            for index, post in enumerate(ranked, 1):
                metric = _metric_text(post)
                suffix = f"　{metric}" if metric else ""
                lines.append(
                    f"{index}. **{_clean(post.title, 70) or '(无标题)'}**"
                    f"　`{platform_label(post.platform)}`{suffix}"
                )
                meta = "　".join(
                    part for part in (
                        f"@{post.author}" if post.author else "",
                        _fmt_time(post.publish_time),
                    ) if part
                )
                lines.append(f"   {meta}")
                if post.url:
                    lines.append(f"   <{post.url}>")
            lines.append("")

        # ---- 分平台明细 ----
        lines.append("## 三、分平台明细")
        lines.append("")
        for platform in ("wechat", "xiaohongshu", "douyin"):
            items = grouped.get(platform)
            if not items:
                continue
            lines.append(f"### {platform_label(platform)}（{len(items)} 条）")
            lines.append("")
            for post in items:
                value, _label = _metric(post)
                lines.append(f"- [{_clean(post.title, 70) or '(无标题)'}]({post.url})"
                             if post.url else
                             f"- {_clean(post.title, 70) or '(无标题)'}")
                meta = "　".join(
                    part for part in (
                        f"@{post.author}" if post.author else "",
                        _fmt_time(post.publish_time),
                        _metric_text(post),
                        f"相关度 {post.relevance:.2f}",
                    ) if part
                )
                lines.append(f"  {meta}")
                if post.content:
                    lines.append(f"  > {_clean(post.content, 110)}")
            lines.append("")

        # ---- 关键词命中分布，用来判断词典是否需要调 ----
        hit_counter: Counter[str] = Counter()
        for post in self.posts:
            hit_counter.update(post.relevance_hits[:4])
        if hit_counter:
            lines.append("## 四、命中关键词分布")
            lines.append("")
            lines.append("　".join(f"{word}({count})" for word, count in hit_counter.most_common(12)))
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("*本报告由 frisbee-radar 自动生成，内容版权归原作者所有，"
                     "仅供内部选题与舆情参考，请勿二次分发。*")
        return "\n".join(lines) + "\n"

    def write(self, path: Path, top_n: int = 10) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_markdown(top_n=top_n), encoding="utf-8")
        return path
