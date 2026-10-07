"""自定义域名命令的测试。

这个命令有两条护栏，都是被真实情况逼出来的：

**护栏一：顺序不能反。**
    GitHub Pages 一旦认了自定义域名，原来的 `<用户名>.github.io/<仓库>/`
    会自动 301 跳到新域名。所以 DNS 没生效就设置，等于把两个地址都弄成
    打不开 —— 网站直接下线。

**护栏二：判据必须是「指向 GitHub Pages」，不是「能解析」。**
    这台机器的 DNS 在做**域名劫持** —— 路由器连 `nonexistent.invalid`
    这种保留域名都能解析出一个 IP。只检查"能不能解析"的话，在劫持 DNS 下
    任意垃圾域名都会通过检查，然后被设上去，网站下线。
    所以判据要求解析结果**指向 <owner>.github.io（CNAME）或 GitHub Pages
    的那 4 个官方 IP（根域名用 A 记录）**。劫持返回的地址不可能是这两者。

测试用注入的 resolver，不碰真实 DNS —— 否则结果取决于这台机器的网络环境，
换台机器就跑不出同样的结论。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frisbee_radar.cli import (  # noqa: E402
    GITHUB_PAGES_IPS,
    _domain_ready_for_pages,
    _resolve_domain,
    do_domain,
)
from frisbee_radar.config import load_config  # noqa: E402

OWNER = "realchenchenluo"
PAGES_TARGET = f"{OWNER}.github.io"


def fake_resolver(addresses=(), cname=""):
    """造一个假的 resolver，返回固定的解析结果。"""
    return lambda domain: (list(addresses), cname)


class TestDomainReadiness(unittest.TestCase):
    def check(self, domain, addresses=(), cname=""):
        return _domain_ready_for_pages(
            domain, OWNER, resolve=fake_resolver(addresses, cname)
        )

    def test_cname_to_github_io_is_ready(self):
        ready, detail, _ = self.check("feipan.info", cname=PAGES_TARGET)
        self.assertTrue(ready, detail)
        self.assertIn(PAGES_TARGET, detail)

    def test_cname_with_trailing_dot_is_ready(self):
        ready, _, _ = self.check("feipan.info", cname=PAGES_TARGET + ".")
        self.assertTrue(ready)

    def test_subdomain_cname_is_ready(self):
        ready, _, _ = self.check("www.feipan.info", cname=PAGES_TARGET)
        self.assertTrue(ready)

    def test_apex_with_github_ips_is_ready(self):
        """根域名不能用 CNAME，配那 4 个官方 IP 也算就绪。"""
        ready, detail, _ = self.check("feipan.info", addresses=["185.199.108.153"])
        self.assertTrue(ready, detail)
        self.assertIn("185.199.108.153", detail)

    def test_partial_github_ips_is_ready(self):
        ready, _, _ = self.check(
            "feipan.info", addresses=["185.199.109.153", "185.199.110.153"]
        )
        self.assertTrue(ready)

    # ---- 下面是关键：不该放行的情况 ----

    def test_hijacked_dns_is_not_ready(self):
        """★ 劫持 DNS 返回一个随便的 IP —— 绝不能放行。

        实测这台机器就是这种情况：连 nonexistent.invalid 都能解析出 IP。
        只要判据是"能解析"，这道护栏就形同虚设。
        """
        ready, detail, _ = self.check("feipan.info", addresses=["192.168.8.1"])
        self.assertFalse(ready, "劫持返回的地址不该被当成配好了")
        self.assertIn("没指向", detail)

    def test_public_ip_that_is_not_github_is_not_ready(self):
        ready, _, _ = self.check("feipan.info", addresses=["1.2.3.4"])
        self.assertFalse(ready)

    def test_cname_to_somewhere_else_is_not_ready(self):
        ready, _, _ = self.check("feipan.info", cname="some.other.host")
        self.assertFalse(ready)

    def test_cname_to_lookalike_domain_is_not_ready(self):
        """`evil-github.io` 这种近似域名不能算通过。"""
        ready, _, _ = self.check("feipan.info", cname="evil-github.io")
        self.assertFalse(ready)

    def test_unresolvable_is_not_ready(self):
        ready, detail, _ = self.check("feipan.info")
        self.assertFalse(ready)
        self.assertIn("解析不出", detail)

    def test_empty_domain_is_not_ready(self):
        for domain in ("", "   ", None):
            ready, _, _ = self.check(domain)
            self.assertFalse(ready, f"{domain!r} 不该就绪")

    def test_garbage_domain_is_not_ready(self):
        for domain in ("这不是域名", "http://x", "a b.com", "."):
            ready, _, _ = self.check(domain)
            self.assertFalse(ready, f"{domain!r} 不该就绪")

    def test_all_four_official_ips_are_recognized(self):
        for ip in GITHUB_PAGES_IPS:
            ready, _, _ = self.check("feipan.info", addresses=[ip])
            self.assertTrue(ready, f"{ip} 应该被认成 GitHub Pages")


class TestResolveDomainInputValidation(unittest.TestCase):
    """_resolve_domain 是唯一碰网络的地方，但它必须先挡掉非法输入。

    实测 socket.getaddrinfo("") 不报错，而是返回**本机**地址
    （172.x / 192.168.x / fe80::…）—— 不挡掉的话空串会被判成"解析成功"。
    """

    def test_invalid_inputs_short_circuit_without_lookup(self):
        for domain in ("", "   ", None, "这不是域名", "a b.com", "-x.com"):
            addresses, cname = _resolve_domain(domain)  # type: ignore[arg-type]
            self.assertEqual(addresses, [], f"{domain!r} 不该去解析")
            self.assertEqual(cname, "")


class TestRefusesWhenDnsNotReady(unittest.TestCase):
    """DNS 没配好时必须拒绝，且不留任何痕迹。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "docs"
        self.out.mkdir(parents=True)
        self.config = load_config()
        object.__setattr__(self.config.publish, "dir", str(self.out))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, domain, *, addresses=(), cname="", clear=False):
        args = argparse.Namespace(domain=domain, clear=clear, repo=None, out=None)
        with mock.patch(
            "frisbee_radar.cli._resolve_domain", fake_resolver(addresses, cname)
        ):
            return do_domain(args, self.config)

    def test_returns_error_and_writes_no_cname(self):
        code = self._run("feipan.info", addresses=["192.168.8.1"])   # 劫持
        self.assertEqual(code, 1, "DNS 没指向 Pages 就不该成功返回")
        self.assertFalse(
            (self.out / "CNAME").exists(),
            "写了 CNAME —— 一旦推送会让网站跳到一个打不开的域名",
        )

    def test_refuses_when_nothing_resolves(self):
        self.assertEqual(self._run("feipan.info"), 1)
        self.assertFalse((self.out / "CNAME").exists())

    def test_missing_domain_argument_explains_usage(self):
        self.assertEqual(self._run(""), 2)

    def test_proceeds_when_dns_is_correct(self):
        """DNS 配对了就该继续（会写 CNAME 文件）。"""
        code = self._run("feipan.info", cname=PAGES_TARGET)
        self.assertEqual(code, 0)
        self.assertTrue((self.out / "CNAME").exists())
        self.assertEqual(
            (self.out / "CNAME").read_text(encoding="utf-8").strip(), "feipan.info"
        )


class TestDomainCommandRegistered(unittest.TestCase):
    def test_subcommand_exists_with_expected_flags(self):
        from frisbee_radar.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["domain", "feipan.info"])
        self.assertEqual(args.command, "domain")
        self.assertEqual(args.domain, "feipan.info")
        self.assertFalse(args.clear)

        args = parser.parse_args(["domain", "--clear"])
        self.assertTrue(args.clear)


class TestCloudflareDeployWiring(unittest.TestCase):
    """Cloudflare 部署要挂在「内容有变化」这个条件上。

    跟「没新内容就什么都不做」同一个原则：没变化就不该产生部署记录。
    """

    def test_config_has_cloudflare_project_field(self):
        cfg = load_config()
        self.assertTrue(hasattr(cfg.publish, "cloudflare_project"))

    def test_shipped_config_points_at_feipannews(self):
        self.assertEqual(load_config().publish.cloudflare_project, "feipannews")

    def test_default_is_empty(self):
        from frisbee_radar.config import PublishSettings

        self.assertEqual(PublishSettings.from_dict(None).cloudflare_project, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
