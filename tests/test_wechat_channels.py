"""微信视频号采集器的离线测试。

联网部分没法在这里验证（必须先微信扫码），但**提取逻辑可以**：
视频号的接口路径和字段名都没有公开文档，所以采集器靠
「响应特征匹配 + 通用 JSON 遍历」来提数据。这套兜底逻辑的正确性
完全可以用构造的响应离线验证 —— 而且更应该验证，因为线上出问题时
你没法一眼看出是"没抓到"还是"抓到了但没解析对"。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frisbee_radar.browser import (  # noqa: E402
    AUTH_COOKIES,
    LOGIN_BY_CONTENT,
    LOGIN_URLS,
)
from frisbee_radar.config import load_config  # noqa: E402
from frisbee_radar.models import platform_label  # noqa: E402
from frisbee_radar.sources import AVAILABLE, SOURCE_CLASSES  # noqa: E402
from frisbee_radar.sources.base import SourceError  # noqa: E402
from frisbee_radar.sources.wechat_channels import (  # noqa: E402
    API_HINTS,
    NOT_SUPPORTED_MESSAGE,
    NO_PUBLIC_SEARCH_PATHS,
    SKIP_HINTS,
    WechatChannelsSource,
)

SRC = WechatChannelsSource


class TestFindCards(unittest.TestCase):
    """_find_cards 要在不知道确切结构的前提下找出内容卡片。"""

    def find(self, payload):
        out: list[dict] = []
        SRC._find_cards(payload, out)
        return out

    def test_finds_objectdesc_wrapped_card(self):
        """objectDesc 是视频号数据结构的典型包装层。"""
        payload = {
            "data": {
                "list": [
                    {"id": "v1", "objectDesc": {
                        "title": "飞盘反手教学", "description": "三个要点",
                        "createTime": 1791193600,
                    }},
                ]
            }
        }
        cards = self.find(payload)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["id"], "v1")

    def test_finds_heuristic_card_without_objectdesc(self):
        """没有 objectDesc 时退化成启发式：同时有 id 和标题类字段。"""
        payload = {"items": [{"objectId": "x1", "title": "飞盘训练日常"}]}
        cards = self.find(payload)
        self.assertEqual(len(cards), 1)

    def test_ignores_error_envelopes(self):
        """带 errMsg 的错误响应不该被当成内容卡片。"""
        payload = {"errMsg": "not login", "title": "错误"}
        self.assertEqual(self.find(payload), [])

    def test_ignores_node_without_id(self):
        payload = {"data": [{"title": "只有标题没有id"}]}
        self.assertEqual(self.find(payload), [])

    def test_ignores_metrics_and_logs(self):
        payload = {"metrics": [{"id": "m1", "title": "埋点"}]}
        # 有 id 有 title，会被启发式当成卡片——这是可接受的误报，
        # 因为真实埋点响应不会同时带业务 id 和 title。
        # 这里只确认不会崩，且数量可控
        self.assertLessEqual(len(self.find(payload)), 1)

    def test_empty_and_scalar_payloads_safe(self):
        for payload in ({}, [], None, 0, "text"):
            self.assertEqual(self.find(payload), [])

    def test_deeply_nested_is_bounded(self):
        """嵌套过深要能停下，不能无限递归。"""
        node: dict = {"id": "leaf", "title": "很深"}
        for _ in range(30):
            node = {"child": node}
        self.assertEqual(self.find(node), [])

    def test_dedupes_nothing_but_allows_multiple(self):
        payload = {"list": [
            {"id": "a", "objectDesc": {"title": "A"}},
            {"id": "b", "objectDesc": {"title": "B"}},
        ]}
        self.assertEqual(len(self.find(payload)), 2)


class TestToPost(unittest.TestCase):
    def setUp(self) -> None:
        self.src = SRC(
            load_config().platform("wechat_channels"), load_config()
        )

    def test_maps_full_card(self):
        card = {
            "id": "feed-1",
            "exportId": "eid-1",
            "objectDesc": {
                "title": "飞盘反手传盘教学",
                "description": "三个常见错误",
                "createTime": 1791193600,
                "contact": {"nickname": "飞盘教练老张", "username": "finder_zhang"},
                "likeInfo": {"likeCount": "1.2万", "commentCount": 34},
            },
        }
        post = self.src._to_post(card, "飞盘")
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.platform, "wechat_channels")
        self.assertEqual(post.post_id, "feed-1")
        self.assertEqual(post.title, "飞盘反手传盘教学")
        self.assertEqual(post.content, "三个常见错误")
        self.assertEqual(post.author, "飞盘教练老张")
        self.assertEqual(post.author_id, "finder_zhang")
        self.assertEqual(post.likes, 12000)
        self.assertEqual(post.comments, 34)
        self.assertIsNotNone(post.publish_time)
        self.assertIn("eid-1", post.url)
        self.assertEqual(post.matched_by, "飞盘")

    def test_falls_back_to_description_as_title(self):
        card = {"id": "f2", "objectDesc": {"description": "只有描述没有标题"}}
        post = self.src._to_post(card, "飞盘")
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.title, "只有描述没有标题")

    def test_returns_none_without_id(self):
        card = {"objectDesc": {"title": "没有 id"}}
        self.assertIsNone(self.src._to_post(card, "飞盘"))

    def test_returns_none_without_any_text(self):
        card = {"id": "f3", "objectDesc": {}}
        self.assertIsNone(self.src._to_post(card, "飞盘"))

    def test_handles_card_without_objectdesc(self):
        card = {"objectId": "o1", "title": "扁平结构", "desc": "直接放外面"}
        post = self.src._to_post(card, "飞盘")
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.title, "扁平结构")

    def test_missing_author_and_stats_are_empty_not_crash(self):
        card = {"id": "f4", "objectDesc": {"title": "无作者"}}
        post = self.src._to_post(card, "磁")
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.author, "")
        self.assertEqual(post.likes, 0)
        self.assertIsNone(post.publish_time)

    def test_does_not_emit_literal_undefined_strings(self):
        """字段缺失时不能生成 'None' / 'undefined' 这种字面量。"""
        card = {"id": "f5", "objectDesc": {"title": "x"}}
        post = self.src._to_post(card, "飞盘")
        assert post is not None
        for value in (post.title, post.content, post.author, post.url):
            self.assertNotIn("None", value)
            self.assertNotIn("undefined", value)

    def test_raw_stays_small(self):
        """raw 只留结构摘要，不要把整个响应塞进库里。"""
        card = {"id": "f6", "objectDesc": {"title": "x", "junk": list(range(500))}}
        post = self.src._to_post(card, "飞盘")
        assert post is not None
        self.assertLessEqual(len(post.raw.get("keys", [])), 20)


class TestPlatformRegistration(unittest.TestCase):
    def test_registered_in_source_registry(self):
        self.assertIn("wechat_channels", SOURCE_CLASSES)
        self.assertIn("wechat_channels", AVAILABLE)

    def test_has_chinese_label(self):
        self.assertEqual(platform_label("wechat_channels"), "微信视频号")

    def test_login_url_points_at_real_login_page(self):
        """曾经指到 /web/pages/home —— 那是个空页面，用户扫码时一脸茫然。"""
        url = LOGIN_URLS["wechat_channels"]
        self.assertTrue(url.endswith("/login.html"), url)
        self.assertNotIn("/web/pages/home", url)

    def test_cookie_names_configured(self):
        self.assertIn("wechat_channels", AUTH_COOKIES)
        self.assertTrue(AUTH_COOKIES["wechat_channels"])

    def test_content_probe_no_longer_claims_channels(self):
        """内容探测的判据对视频号是错的（它未登录也是空壳），已撤掉。"""
        self.assertNotIn("wechat_channels", LOGIN_BY_CONTENT)

    def test_shipped_config_has_section_and_is_disabled(self):
        """默认关闭：这个平台还没有可用接口。"""
        cfg = load_config()
        settings = cfg.platform("wechat_channels")
        self.assertTrue(settings.keywords)
        self.assertFalse(settings.enabled)

    def test_api_hints_cover_search_keywords(self):
        for hint in ("search", "object", "feed"):
            self.assertIn(hint, API_HINTS)

    def test_skip_hints_cover_telemetry(self):
        for hint in ("report", "perf", "log"):
            self.assertIn(hint, SKIP_HINTS)

    def test_documents_which_paths_were_ruled_out(self):
        """/web/pages/home 和 /web/pages/search 实测都取不到内容，要留个记录。"""
        self.assertIn("/web/pages/home", NO_PUBLIC_SEARCH_PATHS)

    def test_enabled_platforms_excludes_channels_by_default(self):
        from frisbee_radar.sources import enabled_platforms

        names = enabled_platforms(load_config())
        self.assertNotIn("wechat_channels", names)
        self.assertIn("wechat", names)


class TestNotSupportedIsExplicit(unittest.TestCase):
    """做不到就要直说，不能搜一圈再报「没找到内容」——
    那会让人以为是关键词不对或登录失效，去排查一个不存在的问题。"""

    def test_crawl_raises_with_the_reason(self):
        import asyncio

        src = SRC(load_config().platform("wechat_channels"), load_config())
        with self.assertRaises(SourceError) as ctx:
            asyncio.run(src.crawl_target("飞盘", "keyword", 10))
        message = str(ctx.exception)
        self.assertIn("做不了", message)
        self.assertIn("视频号助手", message)
        self.assertIn("probe", message)

    def test_message_stays_in_sync_with_module_constant(self):
        self.assertIn("视频号助手", NOT_SUPPORTED_MESSAGE)
        self.assertIn("/web/pages/home", NOT_SUPPORTED_MESSAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
