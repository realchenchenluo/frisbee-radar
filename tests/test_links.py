"""链接可用性检查的测试。

这块的判据是**实测得出**的，不能凭印象改：

  · 公众号失效页：HTTP 200，约 **5~28 KB**（固定的错误页模板）
  · 公众号有效文章：约 **3.4 MB**
  差 100 倍以上，所以用页面大小就能判定，不用开浏览器。

  · 小红书：普通 HTTP 请求**一律被重定向到登录页**，所以所有链接看起来
    都"失效" —— 这个判据完全不能用。要准确判定必须带登录态开浏览器，
    而小红书链接没发现过期现象，花几十次页面加载去验不划算。
    所以标 unknown，卡片上如实说明需要登录才能看。

另外，误判的代价是不对称的：把有效链接误标成失效，读者就不去点了。
所以网络异常一律标 unknown（下次再查），只有确定性的证据才标 dead。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frisbee_radar.links import (  # noqa: E402
    STATUS_DEAD,
    STATUS_OK,
    STATUS_UNKNOWN,
    WECHAT_ALIVE_MIN_BYTES,
    needs_recheck,
)
from frisbee_radar.models import Post  # noqa: E402
from frisbee_radar.storage import Storage  # noqa: E402


class TestLinkCheck(unittest.TestCase):
    def test_threshold_is_safely_above_the_error_page(self):
        """阈值要离失效页很远 —— 实测失效页最大 28KB。"""
        self.assertGreaterEqual(WECHAT_ALIVE_MIN_BYTES, 28_000 * 3)

    def test_threshold_is_safely_below_a_real_article(self):
        """也要离真实文章很远 —— 实测有效页约 3.4MB。"""
        self.assertLessEqual(WECHAT_ALIVE_MIN_BYTES, 3_400_000 / 10)

    def test_status_constants_are_distinct(self):
        self.assertEqual(len({STATUS_OK, STATUS_DEAD, STATUS_UNKNOWN}), 3)


class TestNeedsRecheck(unittest.TestCase):
    """查得太勤是白打平台，所以要能跳过刚查过的。"""

    def test_never_checked_needs_check(self):
        self.assertTrue(needs_recheck(None))

    def test_recently_checked_is_skipped(self):
        self.assertFalse(needs_recheck(datetime.now(timezone.utc) - timedelta(hours=1)))

    def test_stale_needs_check(self):
        self.assertTrue(needs_recheck(datetime.now(timezone.utc) - timedelta(hours=48)))

    def test_boundary_around_default_24h(self):
        self.assertFalse(needs_recheck(datetime.now(timezone.utc) - timedelta(hours=23)))
        self.assertTrue(needs_recheck(datetime.now(timezone.utc) - timedelta(hours=25)))

    def test_naive_datetime_is_handled(self):
        """从库里读出来的可能是 naive datetime，别因此崩掉。"""
        self.assertTrue(
            needs_recheck(datetime.now() - timedelta(hours=48))
        )


class TestStorageLinkTracking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def _post(self, pid: str, platform: str = "wechat", **over) -> Post:
        base = {
            "platform": platform,
            "post_id": pid,
            "title": f"飞盘教学 {pid}",
            "url": f"https://mp.weixin.qq.com/s/{pid}",
            "relevance": 0.9,
        }
        base.update(over)
        return Post(**base)

    def test_migration_adds_link_columns(self):
        cols = {r["name"] for r in self.storage.conn.execute("PRAGMA table_info(posts)")}
        self.assertIn("link_status", cols)
        self.assertIn("link_checked_ts", cols)

    def test_new_posts_need_checking(self):
        self.storage.upsert_many([self._post("a"), self._post("b")])
        self.assertEqual(len(self.storage.posts_needing_link_check(hours=0)), 2)

    def test_only_wechat_is_checked_by_default(self):
        """小红书查不了（请求一律跳登录页），所以默认不查它。"""
        self.storage.upsert_many([
            self._post("w1"),
            self._post("x1", platform="xiaohongshu", url="https://www.xiaohongshu.com/explore/x1"),
        ])
        pending = self.storage.posts_needing_link_check(hours=0)
        self.assertEqual([p.post_id for p in pending], ["w1"])

    def test_checked_links_are_skipped_within_the_window(self):
        self.storage.upsert_many([self._post("a")])

        class R:
            platform = "wechat"
            post_id = "a"
            status = STATUS_OK

        self.storage.update_link_status([R()])
        self.assertEqual(len(self.storage.posts_needing_link_check(hours=24)), 0,
                         "刚查过的还在重复查")
        # 窗口放成 0 小时就该重新查
        self.assertEqual(len(self.storage.posts_needing_link_check(hours=0)), 1)

    def test_status_roundtrips_through_the_model(self):
        self.storage.upsert_many([self._post("a")])

        class R:
            platform = "wechat"
            post_id = "a"
            status = STATUS_DEAD

        self.storage.update_link_status([R()])
        loaded = self.storage.query()[0]
        self.assertEqual(loaded.link_status, STATUS_DEAD)
        self.assertIsNotNone(loaded.link_checked_ts)

    def test_link_stats_counts_unchecked(self):
        self.storage.upsert_many([self._post("a"), self._post("b")])

        class R:
            platform = "wechat"
            post_id = "a"
            status = STATUS_DEAD

        self.storage.update_link_status([R()])
        stats = self.storage.link_stats()
        self.assertEqual(stats.get(STATUS_DEAD), 1)
        self.assertEqual(stats.get("unchecked"), 1)

    def test_posts_without_url_are_not_checked(self):
        self.storage.upsert_many([self._post("a", url="")])
        self.assertEqual(len(self.storage.posts_needing_link_check(hours=0)), 0)


class TestRescoringTakesEffectWithoutRecrawling(unittest.TestCase):
    """相关性是采集时算好存库的，所以光改 keywords.yaml 不影响已有数据。

    发布前重算一遍，改词表就能立刻生效 —— 实测收益很实在：
    撞到一篇奇门遁甲文章讲「鸣法飞盘」（术语同名），往 negative 里
    加个词，重跑发布那条就没了，不用重新采集。
    """

    def test_negative_keyword_removes_existing_item_on_rescore(self):
        from frisbee_radar.relevance import KeywordConfig, RelevanceScorer

        old = KeywordConfig(core=["飞盘"], related=[], negative=[], threshold=0.5)
        new = KeywordConfig(core=["飞盘"], related=[], negative=["奇门"], threshold=0.5)

        title = "奇门新手必避坑:同名不同义!鸣法飞盘支破格VS传统支破格"
        self.assertTrue(RelevanceScorer(old).score(title, "").matched)
        self.assertFalse(
            RelevanceScorer(new).score(title, "").matched,
            "加了负向词之后应当被排除 —— 否则改词表对已有数据无效",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
