"""静态网页生成的离线测试。

这条流水线的关键约定都得测住，尤其是**不发布全文**那条：
它是版权上的取舍（只给标题+摘要+回链），一旦哪天改成发全文
就不该悄悄通过测试。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frisbee_radar.categories import Category, CategoryClassifier  # noqa: E402
from frisbee_radar.config import load_config  # noqa: E402
from frisbee_radar.models import Post  # noqa: E402
from frisbee_radar.site import (  # noqa: E402
    EXCERPT_LIMIT,
    build_index_html,
    build_payload,
    generate_site,
)

TRAINING = Category(key="training", name="训练与技巧", order=1,
                    include=["教学", "技巧", "训练", "小知识"])
TEAM = Category(key="team", name="队伍与赛事动态", order=2,
                include=["俱乐部", "招新", "联赛"])


def make_post(pid: str, **over) -> Post:
    base = {
        "platform": "xiaohongshu",
        "post_id": pid,
        "title": f"飞盘教学 {pid}",
        "content": "一段足够长的正文内容，用来验证网页只发布摘要而不是全文。" * 5,
        "author": "某飞盘号",
        "url": f"https://example.com/{pid}",
        "publish_time": datetime.now(timezone.utc),
        "relevance": 0.8,
        "likes": 100,
    }
    base.update(over)
    return Post(**base)


class TestPayload(unittest.TestCase):
    def setUp(self):
        self.clf = CategoryClassifier([TRAINING, TEAM])

    def test_only_categorized_items_are_published(self):
        posts = [
            make_post("a", title="飞盘小知识｜反手传盘"),
            make_post("b", title="为什么年轻人都不玩飞盘了"),   # 归不了类
        ]
        payload = build_payload(posts, self.clf)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["scanned"], 2)
        self.assertEqual(payload["excluded"], 1)

    def test_does_not_publish_full_text(self):
        """只发摘要，不发全文 —— 这是版权上的取舍，要守住。"""
        long_body = "飞盘教学正文" * 200
        payload = build_payload(
            [make_post("a", title="飞盘教学课", content=long_body)], self.clf
        )
        excerpt = payload["items"][0]["excerpt"]
        self.assertLessEqual(len(excerpt), EXCERPT_LIMIT)
        self.assertNotEqual(excerpt, long_body)
        # 完整的 body 不该出现在 data.json 的任何位置
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("飞盘教学正文" * 60, blob)

    def test_items_sorted_by_time_desc(self):
        now = datetime.now(timezone.utc)
        posts = [
            make_post("old", title="飞盘教学 A", publish_time=now - timedelta(days=30)),
            make_post("new", title="飞盘教学 B", publish_time=now),
            make_post("mid", title="飞盘教学 C", publish_time=now - timedelta(days=3)),
        ]
        payload = build_payload(posts, self.clf)
        self.assertEqual([i["title"][-1] for i in payload["items"]], ["B", "C", "A"])

    def test_undated_items_go_last(self):
        now = datetime.now(timezone.utc)
        posts = [
            make_post("nodate", title="飞盘教学 A", publish_time=None),
            make_post("dated", title="飞盘教学 B", publish_time=now),
        ]
        payload = build_payload(posts, self.clf)
        self.assertEqual(payload["items"][-1]["title"][-1], "A")

    def test_category_order_respected(self):
        posts = [
            make_post("t", title="飞盘教学课"),
            make_post("m", title="飞盘俱乐部招新"),
        ]
        payload = build_payload(posts, self.clf)
        labels = [c["label"] for c in payload["categories"]]
        self.assertEqual(labels, ["训练与技巧", "队伍与赛事动态"])

    def test_platform_counts_and_labels(self):
        posts = [
            make_post("x", title="飞盘教学 A", platform="xiaohongshu"),
            make_post("y", title="飞盘教学 B", platform="wechat", views=500),
        ]
        payload = build_payload(posts, self.clf)
        self.assertEqual(payload["platforms"], {"小红书": 1, "微信公众号": 1})
        wechat = next(i for i in payload["items"] if i["platform"] == "wechat")
        self.assertEqual(wechat["metricLabel"], "阅读")
        self.assertEqual(wechat["metric"], 500)

    def test_excerpt_omitted_when_same_as_title(self):
        body = "飞盘教学课"
        payload = build_payload(
            [make_post("a", title=body, content=body)], self.clf
        )
        self.assertEqual(payload["items"][0]["excerpt"], "")

    def test_dates_reported(self):
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        payload = build_payload(
            [make_post("a", title="飞盘教学", publish_time=now)],
            self.clf, generated_at=now,
        )
        self.assertEqual(payload["generatedDate"], "2026-10-07")
        self.assertEqual(payload["latestDate"], "2026-10-07")


class TestIndexHtml(unittest.TestCase):
    def test_html_escapes_title(self):
        payload = build_payload([], CategoryClassifier([TRAINING]))
        page = build_index_html('<script>alert(1)</script>', payload)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_no_external_cdn_dependency(self):
        """不能依赖 CDN —— 断网或 CDN 挂掉页面就白屏了。"""
        payload = build_payload([], CategoryClassifier([TRAINING]))
        page = build_index_html("测试", payload)
        for pattern in ("cdn.", "unpkg", "jsdelivr", "googleapis", "bootstrapcdn"):
            self.assertNotIn(pattern, page.lower(), f"引入了外部依赖：{pattern}")

    def test_placeholders_all_filled(self):
        """.format() 用漏了会留下 {xxx} 花括号，页面直接错乱。"""
        payload = build_payload([], CategoryClassifier([TRAINING]))
        page = build_index_html("飞盘讯息聚合", payload)
        import re

        leftovers = re.findall(r"\{[a-zA-Z_]+\}", page)
        self.assertFalse(leftovers, f"HTML 里残留未替换的占位符：{leftovers[:5]}")

    def test_inline_js_css_survive_formatting(self):
        payload = build_payload([], CategoryClassifier([TRAINING]))
        page = build_index_html("x", payload)
        # CSS/JS 里大量花括号，模板用了 {{ }} 转义，漏了会把代码破坏掉
        self.assertIn("function render()", page)
        self.assertIn("border-radius", page)


class TestGenerateSite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "docs"
        self.clf = CategoryClassifier([TRAINING, TEAM])

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_expected_files(self):
        info = generate_site(
            [make_post("a", title="飞盘教学课")], self.out, self.clf
        )
        for name in ("index.html", "data.json", ".nojekyll"):
            self.assertTrue((self.out / name).exists(), f"缺 {name}")
        self.assertEqual(info["total"], 1)

    def test_nojekyll_present(self):
        """少了它 GitHub Pages 会用 Jekyll 处理，_ 开头的文件会被吞掉。"""
        generate_site([make_post("a", title="飞盘教学课")], self.out, self.clf)
        self.assertTrue((self.out / ".nojekyll").exists())

    def test_payload_is_valid_json_and_utf8(self):
        generate_site([make_post("a", title="飞盘教学课")], self.out, self.clf)
        raw = (self.out / "data.json").read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["total"], 1)
        # 中文不能被转义成 \uXXXX，否则体积翻几倍
        self.assertIn("飞盘", raw.decode("utf-8"))

    def test_archive_keeps_a_copy(self):
        info = generate_site([make_post("a", title="飞盘教学课")], self.out, self.clf)
        self.assertTrue((self.out / "archive" / f"{info['generatedDate']}.json").exists())

    def test_rerun_produces_same_content_ignoring_timestamp(self):
        """同样输入两次，除了生成时刻以外应该完全一致。

        generatedAt 记的是"什么时候生成的"，两次跑肯定差几毫秒 ——
        所以比较时把它摘掉，比的是内容本身稳不稳定。
        （这也意味着每跑一次发布都会产生一个提交，因为时间戳变了。
          这是有意的：发布历史本身有价值。）
        """
        # 注意要复用同一个 post：make_post 里 publish_time 默认取 now()，
        # 调两次会造出两条时间不同的数据，比出来当然不一样
        posts = [make_post("a", title="飞盘教学课")]

        generate_site(posts, self.out, self.clf)
        first = json.loads((self.out / "data.json").read_text(encoding="utf-8"))
        generate_site(posts, self.out, self.clf)
        second = json.loads((self.out / "data.json").read_text(encoding="utf-8"))

        first.pop("generatedAt"), second.pop("generatedAt")
        self.assertEqual(first, second)

    def test_html_is_identical_across_runs(self):
        """HTML 模板不该带时间戳之类每次都变的东西。"""
        generate_site([make_post("a", title="飞盘教学课")], self.out, self.clf)
        first = (self.out / "index.html").read_text(encoding="utf-8")
        generate_site([make_post("a", title="飞盘教学课")], self.out, self.clf)
        second = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_empty_input_still_produces_a_page(self):
        info = generate_site([], self.out, self.clf)
        self.assertEqual(info["total"], 0)
        self.assertTrue((self.out / "index.html").exists())


class TestSkipIfUnchanged(unittest.TestCase):
    """没有新内容就什么都不做。

    用户明确要求过：「有新的内容就更新，没有就保留原来的」。
    这条护栏对应的是：定时任务每天跑，但没新内容时不能重写文件、
    不能刷新「最近更新」时间、不能产生 git 提交 ——
    否则网页上会显示"刚刚更新"，而实际上什么都没有，等于骗读者。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "docs"
        self.clf = CategoryClassifier([TRAINING, TEAM])

    def tearDown(self):
        self.tmp.cleanup()

    def test_signature_ignores_generated_at(self):
        from frisbee_radar.site import content_signature

        a = {"total": 3, "generatedAt": "2026-10-07 19:00", "items": []}
        b = {"total": 3, "generatedAt": "2026-10-08 21:00", "items": []}
        self.assertEqual(content_signature(a), content_signature(b))

    def test_signature_changes_when_content_changes(self):
        from frisbee_radar.site import content_signature

        a = {"total": 3, "generatedAt": "x", "items": []}
        b = {"total": 4, "generatedAt": "x", "items": []}
        self.assertNotEqual(content_signature(a), content_signature(b))

    def test_signature_changes_when_item_changes(self):
        from frisbee_radar.site import content_signature

        a = {"generatedAt": "x", "items": [{"title": "飞盘教学"}]}
        b = {"generatedAt": "x", "items": [{"title": "飞盘教学（新版）"}]}
        self.assertNotEqual(content_signature(a), content_signature(b))

    def test_second_run_with_same_data_writes_nothing(self):
        posts = [make_post("a", title="飞盘教学课")]
        generate_site(posts, self.out, self.clf)
        before = (self.out / "data.json").stat().st_mtime_ns

        info = generate_site(posts, self.out, self.clf, skip_if_unchanged=True)

        self.assertTrue(info["unchanged"])
        self.assertEqual((self.out / "data.json").stat().st_mtime_ns, before,
                         "内容没变却重写了文件 —— 定时任务会因此产生假更新")

    def test_second_run_reports_existing_counts(self):
        posts = [make_post("a", title="飞盘教学课")]
        generate_site(posts, self.out, self.clf)
        info = generate_site(posts, self.out, self.clf, skip_if_unchanged=True)
        self.assertEqual(info["total"], 1)
        self.assertIn("训练与技巧", info["categories"])

    def test_new_content_does_write(self):
        generate_site([make_post("a", title="飞盘教学 A")], self.out, self.clf)
        info = generate_site(
            [make_post("a", title="飞盘教学 A"), make_post("b", title="飞盘教学 B")],
            self.out, self.clf, skip_if_unchanged=True,
        )
        self.assertFalse(info["unchanged"])
        self.assertEqual(info["total"], 2)

    def test_default_always_writes(self):
        """手动发布（不带 --if-changed）仍然照常重写，行为不变。"""
        posts = [make_post("a", title="飞盘教学课")]
        generate_site(posts, self.out, self.clf)
        info = generate_site(posts, self.out, self.clf)
        self.assertFalse(info["unchanged"])

    def test_corrupt_existing_data_does_not_block_publishing(self):
        """旧的 data.json 坏掉时要照常重写，不能因为读不了就卡住。"""
        generate_site([make_post("a", title="飞盘教学课")], self.out, self.clf)
        (self.out / "data.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
        info = generate_site(
            [make_post("a", title="飞盘教学课")], self.out, self.clf, skip_if_unchanged=True
        )
        self.assertFalse(info["unchanged"])
        json.loads((self.out / "data.json").read_text(encoding="utf-8"))


class TestFallbackLink(unittest.TestCase):
    """公众号链接会过期，得给个「找回来」的入口。

    实测：搜狗给的跳转地址带 `timestamp`+`signature`，采后约 4 小时还能开，
    两天后就不是文章页了。永久链接（`__biz=..&sn=..`）拿不到 ——
    sn 在 HTML 里是空的、由 JS 填，而且真浏览器打开时 `window.sn` 也是空。
    所以退一步：给一个按标题搜索的链接。
    """

    def setUp(self):
        self.clf = CategoryClassifier([TRAINING, TEAM])

    def test_wechat_items_get_a_search_fallback(self):
        payload = build_payload(
            [make_post("a", title="飞盘教学课", platform="wechat",
                       author="某飞盘号")], self.clf,
        )
        fallback = payload["items"][0]["fallbackUrl"]
        self.assertTrue(fallback)
        self.assertIn("weixin.sogou.com", fallback)
        # 查询词要带上标题，否则搜不到
        self.assertIn("飞盘", unquote(fallback))

    def test_other_platforms_have_no_fallback(self):
        """小红书链接实测没有过期问题，不用加这个入口。"""
        payload = build_payload(
            [make_post("b", title="飞盘教学课", platform="xiaohongshu")], self.clf
        )
        self.assertEqual(payload["items"][0]["fallbackUrl"], "")

    def test_fallback_empty_without_title(self):
        payload = build_payload(
            [make_post("c", title="飞盘教学课", platform="wechat")], self.clf
        )
        # 标题为空时不该拼出个没意义的搜索链接
        posts = [make_post("d", title="", platform="wechat")]
        payload = build_payload(posts, self.clf)
        for item in payload["items"]:
            self.assertEqual(item["fallbackUrl"], "")

    def test_page_renders_the_fallback_link(self):
        payload = build_payload([], CategoryClassifier([TRAINING]))
        page = build_index_html("x", payload)
        self.assertIn("fallbackUrl", page)
        self.assertIn("链接打不开", page)


class TestShippedPublishConfig(unittest.TestCase):
    def test_defaults_point_at_docs(self):
        """GitHub Pages 要指到 /docs，配错了网页就是 404。"""
        cfg = load_config()
        self.assertEqual(cfg.publish.dir, "docs")
        self.assertEqual(cfg.publish.branch, "main")
        self.assertTrue(cfg.publish.title)

    def test_resolve_dir_is_absolute(self):
        self.assertTrue(load_config().publish.resolve_dir().is_absolute())


if __name__ == "__main__":
    unittest.main(verbosity=2)
