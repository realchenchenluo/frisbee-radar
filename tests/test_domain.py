"""自定义域名命令的测试。

这个命令有个「顺序不能反」的约束，必须有护栏：

    GitHub Pages 一旦认了自定义域名，原来的 `<用户名>.github.io/<仓库>/`
    会自动 301 跳到新域名。所以如果 DNS 还没生效就设置，等于把两个地址
    都弄成打不开 —— 网站直接下线。

所以命令在动手之前必须先查 DNS，查不到就**什么都不做**并给出操作步骤。
下面测的就是这条护栏。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frisbee_radar.cli import _dns_cname_targets, do_domain  # noqa: E402
from frisbee_radar.config import load_config  # noqa: E402


class TestDnsLookup(unittest.TestCase):
    def test_empty_for_unresolvable_domain(self):
        # RFC 2606 保留域名，保证解析不出来
        self.assertEqual(_dns_cname_targets("nonexistent.invalid"), [])

    def test_empty_for_garbage(self):
        self.assertEqual(_dns_cname_targets(""), [])
        self.assertEqual(_dns_cname_targets("这不是域名"), [])

    def test_resolves_a_real_domain(self):
        """能解析的域名要能查出东西来（否则护栏会把正常域名也拦住）。"""
        targets = _dns_cname_targets("example.com")
        self.assertTrue(targets, "example.com 应该能解析出地址")


class TestRefusesWhenDnsNotReady(unittest.TestCase):
    """DNS 没配好时必须拒绝，且不留下任何痕迹。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "docs"
        self.out.mkdir(parents=True)
        self.config = load_config()
        # 把发布目录指到临时目录，避免碰到真的 docs/
        object.__setattr__(self.config.publish, "dir", str(self.out))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, domain: str, clear: bool = False) -> int:
        args = argparse.Namespace(domain=domain, clear=clear, repo=None, out=None)
        return do_domain(args, self.config)

    def test_returns_error_and_writes_no_cname(self):
        code = self._run("nonexistent.invalid")
        self.assertEqual(code, 1, "DNS 没生效就不该成功返回")
        self.assertFalse(
            (self.out / "CNAME").exists(),
            "DNS 没生效却写了 CNAME —— 一旦推送会让网站跳到一个打不开的域名",
        )

    def test_no_cname_means_current_url_keeps_working(self):
        """没写 CNAME 时，GitHub Pages 继续用默认的 github.io 地址。"""
        self._run("nonexistent.invalid")
        self.assertEqual(list(self.out.glob("CNAME")), [])

    def test_missing_domain_argument_explains_usage(self):
        code = self._run("")
        self.assertEqual(code, 2)


class TestDomainCommandRegistered(unittest.TestCase):
    def test_subcommand_exists_with_expected_flags(self):
        from frisbee_radar.cli import build_parser

        parser = build_parser()
        # 应该能解析出 domain 子命令
        args = parser.parse_args(["domain", "feipan.info"])
        self.assertEqual(args.command, "domain")
        self.assertEqual(args.domain, "feipan.info")
        self.assertFalse(args.clear)

        args = parser.parse_args(["domain", "--clear"])
        self.assertTrue(args.clear)


if __name__ == "__main__":
    unittest.main(verbosity=2)
