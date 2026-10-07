"""离线单元测试，不联网、不登录。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frisbee_radar.models import Post  # noqa: E402
from frisbee_radar.relevance import KeywordConfig, RelevanceScorer  # noqa: E402
from frisbee_radar.report import ReportBuilder  # noqa: E402
from frisbee_radar.sources.base import parse_timestamp, to_int  # noqa: E402
from frisbee_radar.storage import Storage  # noqa: E402

TEST_KEYWORDS = KeywordConfig(
    core=["飞盘", "极限飞盘", "frisbee", "飞盘高尔夫"],
    related=["WFDF", "飞盘联赛", "飞盘俱乐部", "接盘"],
    negative=["硬盘", "SSD", "磁盘阵列"],
    threshold=0.5,
    per_platform_limit=100,
)


def make_post(**overrides) -> Post:
    base = {
        "platform": "wechat",
        "post_id": "p1",
        "title": "飞盘联赛开赛",
        "content": "本届飞盘联赛共有 24 支队伍参赛。",
        "author": "测试号",
        "url": "https://example.com/1",
        "publish_time": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return Post(**base)


class TestRelevance(unittest.TestCase):
    def setUp(self) -> None:
        self.scorer = RelevanceScorer(TEST_KEYWORDS)

    def test_title_hit_scores_high(self):
        result = self.scorer.score("飞盘入门指南", "随便写点正文")
        self.assertTrue(result.matched)
        self.assertGreaterEqual(result.score, 0.6)
        self.assertIn("飞盘", result.hits)

    def test_content_only_hit_is_weaker(self):
        """正文提到飞盘但标题没有 —— 默认阈值下应当被过滤。

        这是刻意的精度优先设计：默认 threshold=0.5 等价于「标题必须命中核心词」，
        否则一篇泛泛的《周末运动推荐》里提一句飞盘也会被捞进来。
        """
        content_only = self.scorer.score("周末运动推荐", "推荐大家试试飞盘")
        title_hit = self.scorer.score("飞盘入门", "")

        self.assertGreater(title_hit.score, content_only.score)
        self.assertTrue(title_hit.matched)
        self.assertFalse(content_only.matched)
        # 但分数确实累加了，把阈值调低就能放行
        self.assertGreater(content_only.score, 0.0)

    def test_lower_threshold_admits_content_only_hits(self):
        loose = RelevanceScorer(
            KeywordConfig(core=["飞盘"], related=[], negative=[], threshold=0.25)
        )
        self.assertTrue(loose.score("周末运动推荐", "推荐大家试试飞盘").matched)

    def test_negative_hard_blocks(self):
        result = self.scorer.score("移动硬盘选购", "飞盘其实是硬盘的误读，SSD 更快")
        self.assertEqual(result.score, 0.0)
        self.assertFalse(result.matched)
        self.assertTrue(result.blocked_by)

    def test_related_words_alone_do_not_match(self):
        # 只有泛户外词、没有飞盘核心词的内容，一分都不该给
        pure = self.scorer.score("露营和桨板", "周末去露营玩桨板，很放松")
        self.assertFalse(pure.matched)
        self.assertEqual(pure.score, 0.0)

        # 相关词只在已有核心词时才累加分数
        bare = self.scorer.score("飞盘开赛", "")
        with_related = self.scorer.score("飞盘开赛", "本届飞盘联赛很精彩")
        self.assertEqual(bare.score, 0.60)
        self.assertGreater(with_related.score, bare.score)
        self.assertIn("飞盘联赛", with_related.hits)

    def test_whitespace_insensitive_for_chinese(self):
        # 「飞 盘 变 了」这种空格强调不该被漏掉
        result = self.scorer.score("飞 盘 变 了", "")
        self.assertTrue(result.matched)
        self.assertGreaterEqual(result.score, 0.6)

    def test_case_insensitive_for_english(self):
        lower = self.scorer.score("ultimate FRISBEE guide", "")
        self.assertTrue(lower.matched)
        self.assertIn("frisbee", lower.hits)

    def test_score_capped_at_one(self):
        result = self.scorer.score(
            "极限飞盘 frisbee 飞盘高尔夫", "飞盘 接盘 WFDF 飞盘联赛 飞盘俱乐部"
        )
        self.assertLessEqual(result.score, 1.0)


class TestHelpers(unittest.TestCase):
    def test_to_int_handles_chinese_units(self):
        self.assertEqual(to_int("1.2万"), 12000)
        self.assertEqual(to_int("3,456"), 3456)
        self.assertEqual(to_int("45210"), 45210)
        self.assertEqual(to_int(1234), 1234)
        self.assertEqual(to_int(None), 0)
        self.assertEqual(to_int(""), 0)

    def test_parse_timestamp_seconds_and_millis(self):
        sec = parse_timestamp(1652883559)
        ms = parse_timestamp(1652883559000)
        self.assertIsNotNone(sec)
        self.assertIsNotNone(ms)
        self.assertEqual(sec.year, 2022)
        self.assertEqual(int(sec.timestamp()), int(ms.timestamp()))

    def test_parse_timestamp_strings(self):
        self.assertEqual(parse_timestamp("2024-05-01") .year, 2024)  # type: ignore[union-attr]
        self.assertEqual(parse_timestamp("2024-05-01 10:30:00").hour, 10)  # type: ignore[union-attr]
        self.assertIsNone(parse_timestamp("不是时间"))
        self.assertIsNone(parse_timestamp(None))


class TestPost(unittest.TestCase):
    def test_content_hash_stable_across_whitespace(self):
        a = make_post(title="飞盘联赛", content="第一行\n第二行")
        b = make_post(title=" 飞盘联赛 ", content="第一行  第二行")
        self.assertEqual(a.content_hash, b.content_hash)

    def test_content_hash_differs_for_different_content(self):
        self.assertNotEqual(
            make_post(title="飞盘A").content_hash,
            make_post(title="飞盘B").content_hash,
        )

    def test_is_valid(self):
        self.assertTrue(make_post().is_valid())
        self.assertFalse(make_post(post_id="").is_valid())
        self.assertFalse(make_post(title="", content="", url="").is_valid())


class TestStorage(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.storage.close()
        self.tmp.cleanup()

    def test_insert_and_count(self):
        ins, upd, skip = self.storage.upsert_many([
            make_post(post_id="p1"),
            make_post(post_id="p2", title="飞盘教练培训", content="培训计划公布了。"),
        ])
        self.assertEqual((ins, upd, skip), (2, 0, 0))
        self.assertEqual(self.storage.stats()["_total"], 2)

    def test_short_identical_texts_not_deduped(self):
        """抖音上一堆视频的文案就是同样的几个话题标签，不能当成同一条。"""
        ins, upd, skip = self.storage.upsert_many([
            make_post(platform="douyin", post_id="v1", title="#飞盘 #极限飞盘", content=""),
            make_post(platform="douyin", post_id="v2", title="#飞盘 #极限飞盘", content=""),
            make_post(platform="douyin", post_id="v3", title="#飞盘 #极限飞盘", content=""),
        ])
        self.assertEqual((ins, upd, skip), (3, 0, 0))
        self.assertEqual(self.storage.stats()["douyin"], 3)

    def test_long_identical_texts_are_deduped(self):
        """长正文的指纹是可靠的，换 post_id 也应识别为同一条。"""
        body = "这是一篇足够长的飞盘赛事报道正文，用来触发正文指纹去重逻辑。" * 2
        ins, upd, skip = self.storage.upsert_many([
            make_post(post_id="a", title="飞盘联赛收官", content=body),
        ])
        self.assertEqual((ins, upd, skip), (1, 0, 0))
        ins, upd, skip = self.storage.upsert_many([
            make_post(post_id="b", title="飞盘联赛收官", content=body),
        ])
        self.assertEqual((ins, upd, skip), (0, 0, 1))
        self.assertEqual(self.storage.stats()["_total"], 1)

    def test_same_post_id_updates_not_duplicates(self):
        self.storage.upsert_many([make_post(likes=1, comments=0)])
        ins, upd, skip = self.storage.upsert_many(
            [make_post(likes=99, comments=0)]
        )
        self.assertEqual((ins, upd, skip), (0, 1, 0))
        self.assertEqual(self.storage.stats()["_total"], 1)
        posts = self.storage.query()
        self.assertEqual(posts[0].likes, 99)

    def test_content_hash_dedups_changed_id(self):
        body = "这是一篇足够长的飞盘赛事报道正文，用来触发正文指纹去重逻辑。" * 2
        self.storage.upsert_many([make_post(post_id="p1", content=body)])
        # 正文相同但 post_id 变了（平台换了链接）应当被识别为重复
        ins, upd, skip = self.storage.upsert_many([make_post(post_id="p2", content=body)])
        self.assertEqual((ins, upd, skip), (0, 0, 1))
        self.assertEqual(self.storage.stats()["_total"], 1)

    def test_invalid_posts_skipped(self):
        ins, upd, skip = self.storage.upsert_many([make_post(post_id="")])
        self.assertEqual((ins, upd, skip), (0, 0, 1))

    def test_duplicates_within_batch_collapsed(self):
        body = "同一轮采集里出现两条完全一样的飞盘赛事报道正文，应当只留一条。" * 2
        ins, upd, skip = self.storage.upsert_many([
            make_post(post_id="p1", title="飞盘联赛收官", content=body),
            make_post(post_id="p2", title="飞盘联赛收官", content=body),
        ])
        self.assertEqual((ins, upd, skip), (1, 0, 1))
        self.assertEqual(self.storage.stats()["_total"], 1)

    def test_query_filters_by_relevance_and_platform(self):
        self.storage.upsert_many([
            make_post(post_id="a", relevance=0.9),
            make_post(post_id="b", relevance=0.1, title="低相关", content="低相关"),
            make_post(post_id="c", platform="douyin", relevance=0.9),
        ])
        self.assertEqual(len(self.storage.query(min_relevance=0.5)), 2)
        self.assertEqual(len(self.storage.query(platforms=["douyin"])), 1)
        self.assertEqual(len(self.storage.query(min_relevance=0.05)), 3)

    def test_query_respects_since(self):
        old = datetime.now(timezone.utc) - timedelta(days=30)
        self.storage.upsert_many([
            make_post(post_id="new"),
            make_post(post_id="old", publish_time=old),
        ])
        recent = self.storage.query(since=datetime.now(timezone.utc) - timedelta(days=7))
        self.assertEqual([p.post_id for p in recent], ["new"])

    def test_roundtrip_preserves_fields(self):
        original = make_post(likes=10, comments=2, collects=3, shares=4, views=5)
        self.storage.upsert_many([original])
        loaded = self.storage.query()[0]
        self.assertEqual(loaded.title, original.title)
        self.assertEqual(loaded.likes, 10)
        self.assertEqual(loaded.views, 5)
        self.assertEqual(loaded.relevance_hits, original.relevance_hits)

    def test_exports(self):
        self.storage.upsert_many([make_post()])
        posts = self.storage.query()
        csv_path = self.storage.export_csv(posts, Path(self.tmp.name) / "o.csv")
        jsonl_path = self.storage.export_jsonl(posts, Path(self.tmp.name) / "o.jsonl")
        self.assertIn("飞盘联赛", csv_path.read_text(encoding="utf-8-sig"))
        self.assertIn("飞盘联赛", jsonl_path.read_text(encoding="utf-8"))


class TestReport(unittest.TestCase):
    def _scored(self) -> list[Post]:
        scorer = RelevanceScorer(TEST_KEYWORDS)
        posts = [
            make_post(post_id="1", title="飞盘联赛收官", views=5000),
            make_post(post_id="2", platform="douyin", title="飞盘教学", likes=3000),
            make_post(post_id="3", title="硬盘评测", content="", likes=999999),
        ]
        for post in posts:
            result = scorer.score(post.title, post.content)
            post.relevance = result.score
            post.relevance_hits = result.hits
        return posts

    def test_markdown_contains_expected_sections(self):
        md = ReportBuilder(self._scored()).to_markdown()
        self.assertIn("# 飞盘讯息简报", md)
        self.assertIn("平台概览", md)
        self.assertIn("分平台明细", md)
        self.assertIn("微信公众号", md)
        self.assertIn("抖音", md)

    def test_irrelevant_content_excluded(self):
        md = ReportBuilder(self._scored()).to_markdown()
        # 硬盘那条 relevance=0，不该出现在报告里
        self.assertNotIn("硬盘评测", md)

    def test_empty_input_renders_hint(self):
        md = ReportBuilder([]).to_markdown()
        self.assertIn("没有筛出飞盘相关内容", md)


class TestMockPipeline(unittest.TestCase):
    """走一遍 样例源 -> 打分 -> 入库 -> 报告。"""

    def test_end_to_end(self):
        import asyncio

        from frisbee_radar.config import load_config
        from frisbee_radar.sources import build_source

        config = load_config()
        scorer = RelevanceScorer(config.keywords)

        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "e2e.db")
            try:
                for platform in ("wechat", "xiaohongshu", "douyin"):
                    source = build_source("mock", config)
                    source.settings.extra["platform"] = platform

                    async def run() -> list[Post]:
                        out: list[Post] = []
                        async for _target, _kind, posts in source.crawl():
                            out.extend(posts)
                        return out

                    posts = asyncio.run(run())
                    self.assertTrue(posts, f"{platform} 样例数据为空")

                    for post in posts:
                        result = scorer.score(post.title, post.content)
                        post.relevance = result.score
                        post.relevance_hits = result.hits
                    storage.upsert_many([p for p in posts if p.relevance > 0])

                stats = storage.stats()
                self.assertEqual(set(stats) - {"_total"}, {"wechat", "xiaohongshu", "douyin"})

                md = ReportBuilder(storage.query()).to_markdown()
                self.assertIn("飞盘", md)
                # 样例里的三类噪音都该被挡在报告外
                self.assertNotIn("移动硬盘选购指南", md)
                self.assertNotIn("周末露营", md)
                self.assertNotIn("极限运动合集", md)
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
