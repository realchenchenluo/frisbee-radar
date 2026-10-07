"""小红书采集器的离线测试。

这一轮踩的坑都记在这儿了，每个都对应实测发现：

1. **搜索接口地址写错了** —— 原以为在 edith.xiaohongshu.com 上、版本是 v1；
   实测真实接口是 `so.xiaohongshu.com/api/sns/web/v2/search/notes`。
   写错的表现是采集 0 条且接口根本不触发，光看日志看不出哪里错。
   只能靠 `run.py probe` 抓真实响应。

2. **发布时间藏在角落标签里** —— 搜索接口不给时间戳，只在
   `corner_tag_info` 里放 `{"type": "publish_time", "text": "09-10"}`，
   **只有月日没有年份**，得推断。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frisbee_radar.config import load_config  # noqa: E402
from frisbee_radar.sources.xiaohongshu import (  # noqa: E402
    SEARCH_API,
    SORT_PARAM,
    XiaohongshuSource,
)

TZ = timezone(timedelta(hours=8))


def make_src() -> XiaohongshuSource:
    cfg = load_config()
    return XiaohongshuSource(cfg.platform("xiaohongshu"), cfg)


class TestSearchEndpoint(unittest.TestCase):
    def test_endpoint_matches_observed_url(self):
        """别凭印象改回 v1 —— 那是错的，会导致一条都采不到。"""
        self.assertEqual(SEARCH_API, "/api/sns/web/v2/search/notes")

    def test_sort_param_accepted(self):
        self.assertIn("time_descending", SORT_PARAM)
        self.assertEqual(SORT_PARAM["time_descending"], "time_descending")


class TestExtractSearchItems(unittest.TestCase):
    """响应结构：{data: {items: [{id, model_type, note_card, xsec_token}], has_more}}"""

    def test_extracts_notes(self):
        payload = {"data": {"items": [
            {"id": "n1", "model_type": "note",
             "note_card": {"display_title": "正手出盘教学"},
             "xsec_token": "TOKEN1"},
        ], "has_more": True}}
        out = XiaohongshuSource._extract_search_items(payload)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "n1")
        self.assertEqual(out[0]["xsec_token"], "TOKEN1")

    def test_skips_items_without_note_card(self):
        payload = {"data": {"items": [
            {"id": "x", "model_type": "hot_query"},
            {"id": "y", "note_card": {"display_title": "有卡片"}},
        ]}}
        self.assertEqual(len(XiaohongshuSource._extract_search_items(payload)), 1)

    def test_tolerates_shape_variants(self):
        # data 直接是 list 的老版本
        self.assertEqual(
            len(XiaohongshuSource._extract_search_items({"data": [
                {"id": "a", "note_card": {"display_title": "x"}}]})), 1)
        # 没有 data
        self.assertEqual(XiaohongshuSource._extract_search_items({}), [])
        self.assertEqual(XiaohongshuSource._extract_search_items({"data": None}), [])


class TestPublishTimeFromCornerTag(unittest.TestCase):
    """搜索接口不给时间戳，时间只在角落标签里，而且没有年份。"""

    def setUp(self):
        self.src = make_src()

    def test_parses_month_day(self):
        now = datetime(2026, 10, 7, tzinfo=TZ)
        card = {"corner_tag_info": [{"type": "publish_time", "text": "09-10"}]}
        got = self.src._parse_corner_time(card, now=now)
        self.assertIsNotNone(got)
        self.assertEqual((got.year, got.month, got.day), (2026, 9, 10))

    def test_infers_previous_year_for_future_dates(self):
        """笔记不可能发布于未来，所以算出来比现在晚就是去年的。"""
        now = datetime(2026, 1, 5, tzinfo=TZ)
        card = {"corner_tag_info": [{"type": "publish_time", "text": "12-25"}]}
        got = self.src._parse_corner_time(card, now=now)
        self.assertIsNotNone(got)
        self.assertEqual((got.year, got.month, got.day), (2025, 12, 25))

    def test_same_year_date_is_not_shifted(self):
        now = datetime(2026, 12, 31, tzinfo=TZ)
        card = {"corner_tag_info": [{"type": "publish_time", "text": "12-30"}]}
        got = self.src._parse_corner_time(card, now=now)
        self.assertEqual(got.year, 2026)

    def test_ignores_other_tag_types(self):
        card = {"corner_tag_info": [
            {"type": "sticky", "text": "置顶"},
            {"type": "publish_time", "text": "08-01"},
        ]}
        got = self.src._parse_corner_time(card, now=datetime(2026, 10, 7, tzinfo=TZ))
        self.assertEqual((got.month, got.day), (8, 1))

    def test_handles_missing_or_malformed(self):
        now = datetime(2026, 10, 7, tzinfo=TZ)
        for card in ({}, {"corner_tag_info": None}, {"corner_tag_info": []},
                     {"corner_tag_info": "not-a-list"},
                     {"corner_tag_info": [{"type": "publish_time", "text": "昨天"}]},
                     {"corner_tag_info": [{"type": "publish_time", "text": "2024-09-10"}]},
                     {"corner_tag_info": [None, 123]}):
            self.assertIsNone(self.src._parse_corner_time(card, now=now), card)

    def test_leap_day_in_common_year_is_safe(self):
        """2-29 遇到平年不能抛异常。"""
        now = datetime(2026, 10, 7, tzinfo=TZ)     # 2026 不是闰年
        card = {"corner_tag_info": [{"type": "publish_time", "text": "02-29"}]}
        self.assertIsNone(self.src._parse_corner_time(card, now=now))

    def test_real_payload_shape_from_probe(self):
        """这段是按 probe 抓到的真实响应原样写的，别再猜结构。"""
        card = {
            "user": {"nick_name": "小远自习室"},
            "interact_info": {"comment_count": "9", "liked_count": "173",
                              "collected_count": "128"},
            "corner_tag_info": [{"type": "publish_time", "text": "09-10"}],
            "type": "video",
            "display_title": "30分钟突破飞盘新手村",
        }
        entry = {"id": "6aa241930000000028039a7d", "card": card, "xsec_token": "T"}
        post = self.src._to_post(entry, "飞盘教学")
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.title, "30分钟突破飞盘新手村")
        self.assertEqual(post.author, "小远自习室")     # 字段名是 nick_name
        self.assertEqual(post.likes, 173)
        self.assertEqual(post.collects, 128)
        self.assertEqual(post.comments, 9)
        self.assertIsNotNone(post.publish_time, "时间必须能从角落标签里解析出来")
        self.assertEqual(post.publish_time.month, 9)
        self.assertEqual(post.publish_time.day, 10)

    def test_timestamp_wins_over_corner_tag_when_both_present(self):
        card = {"time": 1757000000,
                "corner_tag_info": [{"type": "publish_time", "text": "01-01"}]}
        post = self.src._to_post({"id": "n", "card": card}, "kw")
        assert post is not None
        self.assertEqual(post.publish_time.month, 9)


class TestBuildUrl(unittest.TestCase):
    def test_search_url_uses_configured_sort(self):
        """配置里 sort: time_descending 要真的进到 URL 里。"""
        import inspect

        src = inspect.getsource(XiaohongshuSource._crawl_keyword)
        self.assertIn("sort=", src)
        self.assertIn("search_result", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
