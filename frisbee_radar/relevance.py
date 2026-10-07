"""飞盘相关性打分。

三个平台的搜索接口都会被关键词「飞盘」带出大量噪音（硬盘、磁盘阵列、
「飞盘乱飞」之类的比喻），公众号尤其明显，因为搜狗是按全文匹配的。
所以在入库前做一次打分，把噪音挡在报告之外。

打分规则（可调，见 config/keywords.yaml）：

    标题命中核心词          +0.60
    正文命中核心词          +0.25
    命中相关词（需先有核心词）+0.03/个，上限 +0.15
    命中负向词              → 直接 0 分
    标题命中的核心词数 > 1  +0.05（更强信号）

score 落在 0~1，>= threshold 判定为飞盘相关。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class KeywordConfig:
    core: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    threshold: float = 0.5
    per_platform_limit: int = 100

    @classmethod
    def from_dict(cls, data: dict | None) -> "KeywordConfig":
        data = data or {}
        return cls(
            core=[str(w).strip() for w in (data.get("core") or []) if str(w).strip()],
            related=[str(w).strip() for w in (data.get("related") or []) if str(w).strip()],
            negative=[str(w).strip() for w in (data.get("negative") or []) if str(w).strip()],
            threshold=float(data.get("threshold", 0.5)),
            per_platform_limit=int(data.get("per_platform_limit", 100)),
        )


@dataclass
class RelevanceResult:
    score: float
    hits: list[str]
    matched: bool
    blocked_by: str = ""


def _compact(text: str) -> str:
    """去掉全部空白。

    中文内容里常用空格做强调（「飞 盘 变 了」「极 限 飞 盘」），
    不去掉的话子串匹配会整条漏掉。顺带把全角空格也处理了。
    """
    return "".join(text.split()).replace("\u3000", "")


class RelevanceScorer:
    """把一段文本打成 0~1 的飞盘相关度。

    匹配前统一转小写（兼顾 frisbee / disc golf 的大小写）并去掉空白
    （兼顾中文空格强调），所以返回的命中词是词典里的原始写法。
    """

    def __init__(self, config: KeywordConfig):
        self.config = config
        # 预排序：长词优先，避免「飞盘」先命中导致「极限飞盘」这类长词漏记
        self._core = sorted(config.core, key=len, reverse=True)
        self._related = sorted(config.related, key=len, reverse=True)
        self._negative = sorted(config.negative, key=len, reverse=True)

    @staticmethod
    def _find_all(text: str, words: list[str]) -> list[str]:
        # 词典里的词本身也可能带空格（如 "ultimate frisbee"），同样要压平
        return [w for w in words if _compact(w.lower()) in text]

    def score(self, title: str, content: str = "") -> RelevanceResult:
        title_l = _compact((title or "").lower())
        content_l = _compact((content or "").lower())
        # 整段文本用于负向词判断——负向词出现在哪都说明跑偏了
        whole = f"{title_l}\n{content_l}"

        blocked = self._find_all(whole, self._negative)
        if blocked:
            return RelevanceResult(score=0.0, hits=[], matched=False, blocked_by=blocked[0])

        title_hits = self._find_all(title_l, self._core)
        content_hits = [w for w in self._find_all(content_l, self._core) if w not in title_hits]

        value = 0.0
        hits: list[str] = []

        if title_hits:
            value += 0.60
            hits.extend(title_hits)
            if len(title_hits) > 1:
                value += 0.05
        if content_hits:
            value += 0.25
            hits.extend(content_hits)

        # 相关词只在已经有核心词时才计分——否则「露营」「匹克球」这类
        # 泛户外词会把整个平台的内容都捞进来
        if value > 0:
            related_hits = self._find_all(whole, self._related)
            if related_hits:
                value += min(0.15, 0.03 * len(related_hits))
                hits.extend(related_hits)

        value = round(min(1.0, value), 4)
        return RelevanceResult(
            score=value,
            hits=hits,
            matched=value >= self.config.threshold,
        )
