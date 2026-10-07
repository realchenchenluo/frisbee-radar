"""内容分类。

用户要的不是「所有飞盘相关的都收」，而是：

    1. 训练 / 技巧类         —— 最想看
    2. 飞盘队、俱乐部日常     —— 其次
    3. 其它（媒体评论、行业分析、装备种草、蹭热点）—— 不收

所以这里是个白名单分类器：命中不了任何分类的内容，直接不进简报。
这比「全部收进来再排个序」更符合「无关训练的就不要放进来」。

匹配策略是标题优先 + 可选的正文兜底：
  - 标题命中 include 里的词 → 归入该分类
  - 标题命中 exclude 里的词 → 直接判为不该收录，不看 include
  - 正文兜底默认关闭（body_min_hits=0）。实测开启后「新京报」那种
    媒体科普稿会因为正文里出现「教练」「新手」被误收进「训练与技巧」，
    所以默认只认标题，精度优先。

分类顺序由配置里的 order 决定，第一个命中的分类胜出。配合 exclude 就能
表达「带『俱乐部』的一律归队伍动态」这类规则——比如
「一个神秘掷准飞盘俱乐部现身」不会被「掷准」拉进训练。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _compact(text: str) -> str:
    """去空白 + 转小写，和 relevance.py 的口径保持一致。"""
    return "".join(str(text or "").split()).replace("\u3000", "").lower()


@dataclass
class Category:
    key: str
    name: str
    order: int = 100
    include: list[str] = field(default_factory=list)
    # 命中这些词就不归入本分类（也用于把媒体评论类文章挡在门外）
    exclude: list[str] = field(default_factory=list)
    # 简报里这个分类的引导语
    intro: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Category":
        def words(key: str) -> list[str]:
            return [str(w).strip() for w in (data.get(key) or []) if str(w).strip()]

        return cls(
            key=str(data.get("key") or "").strip(),
            name=str(data.get("name") or "").strip(),
            order=int(data.get("order", 100)),
            include=words("include"),
            exclude=words("exclude"),
            intro=str(data.get("intro") or "").strip(),
        )

    def matches_exclude(self, title: str) -> list[str]:
        title_c = _compact(title)
        return [w for w in self.exclude if _compact(w) in title_c]


@dataclass
class CategoryMatch:
    category: Category
    hits: list[str]
    matched_in: str          # "title" | "content"


class CategoryClassifier:
    def __init__(
        self,
        categories: list[Category],
        *,
        body_min_hits: int = 0,
        global_exclude: list[str] | None = None,
    ):
        # order 小的优先；同 order 保持配置里的先后
        self.categories = sorted(
            [c for c in categories if c.key and c.include],
            key=lambda c: c.order,
        )
        self.body_min_hits = max(0, body_min_hits)
        self.global_exclude = [w for w in (global_exclude or []) if w]

    def _is_globally_excluded(self, title: str) -> str:
        title_c = _compact(title)
        for word in self.global_exclude:
            if _compact(word) in title_c:
                return word
        return ""

    def classify(self, title: str, content: str = "") -> CategoryMatch | None:
        blocked = self._is_globally_excluded(title)
        if blocked:
            return None

        title_c = _compact(title)

        for category in self.categories:
            # 排除词优先于包含词：「…俱乐部…」不会被「掷准」拉进训练
            if category.matches_exclude(title):
                continue
            hits = [w for w in category.include if _compact(w) in title_c]
            if hits:
                return CategoryMatch(category, hits, "title")

        # 正文兜底默认关闭，理由见模块 docstring
        if self.body_min_hits <= 0:
            return None

        content_c = _compact(content)
        for category in self.categories:
            if category.matches_exclude(title):
                continue
            hits = [w for w in category.include if _compact(w) in content_c]
            if len(hits) >= self.body_min_hits:
                return CategoryMatch(category, hits, "content")

        return None

    def explain(self, title: str, content: str = "") -> str:
        """给排障用：说清一条内容为什么没被收录。"""
        blocked = self._is_globally_excluded(title)
        if blocked:
            return f"命中全局排除词「{blocked}」"
        title_c = _compact(title)
        for category in self.categories:
            hits = category.matches_exclude(title)
            if hits:
                return f"命中「{category.name}」的排除词「{hits[0]}」"
        best = 0
        for category in self.categories:
            best = max(best, len([w for w in category.include if _compact(w) in title_c]))
        if best == 0:
            return "标题里没有任何分类关键词"
        return "标题关键词不足以归类"

    # 便利方法：把一批内容按分类分组，组内按发布时间倒序（最新的在前）
    def group(self, posts: list[Any]) -> dict[str, list[Any]]:
        grouped, _excluded = self.split(posts)
        return grouped

    def split(self, posts: list[Any]) -> tuple[dict[str, list[Any]], list[Any]]:
        """切分成交类分组 和 未归类内容。

        未归类的那部分不是「排序靠后」，而是不进日报。之所以要把它返回给
        调用方，是为了让调用方能把它标记成「已处理」——否则这些内容会
        永远留在未报送状态，每次都把 watch 判定为「有新内容」。
        """
        buckets: dict[str, list[Any]] = {c.key: [] for c in self.categories}
        excluded: list[Any] = []
        placed: set[int] = set()

        for post in posts:
            match = self.classify(post.title, post.content)
            if match is None:
                excluded.append(post)
                continue
            buckets[match.category.key].append(post)
            placed.add(id(post))

        for key in buckets:
            # 时间缺失的排到最后，而不是被丢掉
            buckets[key].sort(
                key=lambda p: p.publish_time.timestamp() if p.publish_time else 0,
                reverse=True,
            )
        return {k: v for k, v in buckets.items() if v}, excluded
