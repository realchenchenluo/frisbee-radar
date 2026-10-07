"""命令行入口。

    python run.py login  --platform xiaohongshu
    python run.py crawl  --source xiaohongshu
    python run.py crawl                       # 跑配置里所有启用的平台
    python run.py report --days 7
    python run.py export --format csv
    python run.py stats
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from .browser import (
    LOGIN_CLOSED,
    LOGIN_OK,
    LOGIN_URLS,
    browser_context,
    verify_login,
    wait_for_login,
)
from .categories import CategoryClassifier
from .config import PROJECT_ROOT, AppConfig, load_config
from .docx_report import build_docx_daily
from .models import Post, platform_label
from .relevance import RelevanceScorer
from .report import ReportBuilder
from .site import generate_site
from .sources import AVAILABLE, build_source, enabled_platforms
from .sources.base import BaseSource, SourceError
from .storage import Storage

# --------------------------------------------------------------------- 输出


def info(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"[!] {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"[+] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"[x] {msg}", file=sys.stderr, flush=True)


def header(msg: str) -> None:
    print(f"\n{'=' * 62}\n  {msg}\n{'=' * 62}", flush=True)


def resolve_db(config: AppConfig, args: argparse.Namespace) -> Path:
    """定位数据库文件。

    默认 frisbee.db；--mock 流程写的是 mock.db，用 --db 指过去即可，
    这样离线演示不会污染真实数据。
    """
    override = getattr(args, "db", None)
    if override:
        return Path(override)
    return config.data_dir / "frisbee.db"


# --------------------------------------------------------------------- login


async def do_login(
    platform: str, config: AppConfig, timeout: int, cdp: str = ""
) -> int:
    if platform not in LOGIN_URLS:
        fail(f"未知平台 {platform!r}。可选：{', '.join(LOGIN_URLS)}")
        return 2

    header(f"登录 {platform_label(platform)}")
    info(f"即将打开浏览器，请在窗口中完成扫码登录（最多等 {timeout} 秒）。")
    info(f"登录态保存位置：{config.browser_state_dir / platform}")
    info("提示：这里不会接触你的账号密码，也不做验证码绕过，只保存登录后的 cookie。")
    info("")

    async with browser_context(
        platform, config.browser_state_dir, headless=False,
        cdp_url=cdp or "",
    ) as context:
        # 必须用 verify_login：小红书给匿名访客也种 web_session，
        # 只看 cookie 会误判成已登录，然后用户就被这句「无需重复登录」卡住
        if await verify_login(context, platform):
            ok(f"{platform_label(platform)} 已经是登录状态，无需重复登录。")
            info("（如需切换账号，删除 data/browser_state/"
                 f"{platform}/ 后重试）")
            return 0

        def on_wait(remain: int) -> None:
            if remain % 15 == 0 and remain > 0:
                info(f"  …等待扫码中，剩余 {remain} 秒")

        outcome = await wait_for_login(
            context, platform, timeout_sec=timeout, on_wait=on_wait,
            debug_dir=config.data_dir / "login_debug",
        )

        if outcome == LOGIN_OK:
            ok(f"{platform_label(platform)} 登录成功，登录态已保存。")
            return 0

        if outcome == LOGIN_CLOSED:
            # 和超时分开报：用户是主动关的窗口，提示「超时」会让人以为要等到时间耗尽
            fail("登录窗口被关闭了，登录态没保存。")
            shots = sorted((config.data_dir / "login_debug").glob(f"{platform}-*.png"))
            if shots:
                info(f"窗口关闭前的截图留在：{shots[-1].parent}")
            info("如果想重来，重新执行本命令即可。")
            return 1

        fail("等待超时，未检测到登录态。请重新执行本命令。")
        shots = sorted((config.data_dir / "login_debug").glob(f"{platform}-*.png"))
        if shots:
            info(f"超时时的窗口截图：{shots[-1]}")
            info("（把这张图发我，能看出是二维码没出来、过期了，还是扫了没确认）")
        return 1


# --------------------------------------------------------------------- crawl


async def _crawl_source(
    source: BaseSource,
    config: AppConfig,
    storage: Storage | None,
    scorer: RelevanceScorer,
    dry_run: bool,
    only: str | None,
    quiet_filter: bool,
) -> tuple[int, int, int, int, int]:
    """跑一个平台，返回 (抓取数, 飞盘相关数, 新增, 更新, 重复)。"""
    fetched = kept = inserted = updated = duplicated = 0
    started = int(datetime.now(timezone.utc).timestamp())

    try:
        async for target, kind, posts in source.crawl(only=only):
            label = "关键词" if kind == "keyword" else "账号"
            if not posts:
                warn(f"[{source.platform}] {label}「{target}」没有拿到内容")
                if storage is not None and not dry_run:
                    storage.log_run(source.platform, target, 0, 0, 0, "empty", started_ts=started)
                continue

            fetched += len(posts)

            relevant: list[Post] = []
            dropped: list[Post] = []
            for post in posts:
                result = scorer.score(post.title, post.content)
                post.relevance = result.score
                post.relevance_hits = result.hits
                (relevant if result.matched else dropped).append(post)

            kept += len(relevant)
            info(
                f"[{source.platform}] {label}「{target}」抓到 {len(posts)} 条，"
                f"飞盘相关 {len(relevant)} 条"
            )

            if quiet_filter and dropped:
                for post in dropped[:3]:
                    info(f"    · 已过滤：{post.title[:48]}")

            if dry_run:
                for post in relevant[:5]:
                    print(f"    → [{post.relevance:.2f}] {post.title[:60]}")
                    print(f"      {post.url}")
                if storage is not None:
                    storage.log_run(
                        source.platform, target, len(posts), 0, len(relevant),
                        "dry-run", started_ts=started,
                    )
                continue

            if storage is not None:
                ins, upd, skip = storage.upsert_many(relevant)
                inserted += ins
                updated += upd
                duplicated += skip
                storage.log_run(
                    source.platform, target, len(posts), ins, len(relevant),
                    "ok", started_ts=started,
                )
    finally:
        await source.close()

    return fetched, kept, inserted, updated, duplicated


async def do_crawl(args: argparse.Namespace, config: AppConfig) -> int:
    scorer = RelevanceScorer(config.keywords)

    if args.source:
        names = [args.source]
    elif args.mock:
        names = ["mock"]
    else:
        names = enabled_platforms(config)

    if not names:
        fail("没有启用的平台。请在 config/sources.yaml 里把 enabled 设为 true，"
             "或直接用 --mock 跑一遍离线流程。")
        return 2

    header(f"开始采集：{', '.join(platform_label(n) for n in names)}"
           + ("（试运行，不写库）" if args.dry_run else ""))

    storage: Storage | None = None
    if not args.dry_run:
        # 样例数据单独一个库文件，免得把真实采集结果搅浑
        db = config.data_dir / ("mock.db" if names == ["mock"] else "frisbee.db")
        storage = Storage(db)
        info(f"数据库：{db}")

    total_fetched = total_kept = total_inserted = total_updated = total_dup = 0
    exit_code = 0

    for name in names:
        source = build_source(name, config)
        # 样例源按平台切片，模拟哪个平台由 --mock-as 决定
        if name == "mock":
            simulated = getattr(args, "mock_as", None) or "wechat"
            source.settings.extra["platform"] = simulated
            info(f"\n--- 样例数据（模拟 {platform_label(simulated)}）---")
        else:
            info(f"\n--- {platform_label(name)} ---")
        try:
            fetched, kept, inserted, updated, dup = await _crawl_source(
                source, config, storage, scorer,
                dry_run=args.dry_run, only=args.only, quiet_filter=args.verbose,
            )
            total_fetched += fetched
            total_kept += kept
            total_inserted += inserted
            total_updated += updated
            total_dup += dup
        except SourceError as exc:
            fail(f"{platform_label(name)} 采集中断：{exc}")
            exit_code = 1
        except KeyboardInterrupt:
            warn("已手动中断。")
            exit_code = 130
            break
        except Exception as exc:  # noqa: BLE001 - 单平台异常不该影响其他平台
            fail(f"{platform_label(name)} 出现未预期错误：{type(exc).__name__}: {exc}")
            exit_code = 1

    if storage is not None:
        storage.close()

    header("采集完成")
    info(f"抓取 {total_fetched} 条　飞盘相关 {total_kept} 条　"
         f"新增 {total_inserted} 条　更新 {total_updated} 条　重复跳过 {total_dup} 条")
    if total_kept == 0 and not args.dry_run:
        warn("没有筛出飞盘相关内容。如果确认应该命中，检查 config/keywords.yaml "
             "的 threshold 是否偏高。")
    return exit_code


# -------------------------------------------------------------------- report


def do_report(args: argparse.Namespace, config: AppConfig) -> int:
    db = resolve_db(config, args)
    if not db.exists():
        fail(f"数据库不存在：{db}\n先执行一次 python run.py crawl")
        return 2

    with Storage(db) as storage:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        platforms = [args.platform] if args.platform else None
        posts = storage.query(
            platforms=platforms,
            since=since,
            min_relevance=args.min_relevance,
            limit=args.limit,
        )

        if not posts:
            warn(f"最近 {args.days} 天没有符合条件的记录。")

            # 搜狗按相关性排序，返回的多是 2022 年飞盘爆火期的文章，
            # 所以「库里明明有数据但时间窗太窄」是新手最容易卡住的地方。
            total = storage.stats().get("_total", 0)
            info("")
            if not total:
                info("库里还没有数据，先跑一次采集：python run.py crawl --source wechat")
                return 1

            info(f"库里共有 {total} 条记录。")
            oldest = storage.query(min_relevance=args.min_relevance, limit=1, ascending=True)
            newest = storage.query(min_relevance=args.min_relevance, limit=1)
            span = sorted(
                p.publish_time.strftime("%Y-%m-%d")
                for p in (*oldest, *newest)
                if p.publish_time
            )
            if span:
                info(f"这些内容的发布时间落在 {span[0]} ~ {span[-1]}。")
            info(f"放宽时间窗再试：python run.py report --days 3650")
            return 1

        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        out = Path(args.output) if args.output else config.data_dir / "reports" / f"飞盘简报-{stamp}.md"
        title = args.title or f"飞盘讯息简报（近 {args.days} 天）"
        ReportBuilder(posts, title=title).write(out, top_n=args.top)

    ok(f"报告已生成：{out}")
    info(f"共 {len(posts)} 条飞盘相关讯息。直接用 Markdown 阅读器或 Typora 打开即可。")
    return 0


# -------------------------------------------------------------------- export


def do_export(args: argparse.Namespace, config: AppConfig) -> int:
    db = resolve_db(config, args)
    if not db.exists():
        fail(f"数据库不存在：{db}")
        return 2

    with Storage(db) as storage:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        posts = storage.query(since=since, min_relevance=args.min_relevance, limit=args.limit)

        if not posts:
            warn("没有符合条件的记录。")
            return 1

        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        if args.format == "csv":
            out = config.data_dir / "exports" / f"飞盘讯息-{stamp}.csv"
            storage.export_csv(posts, out)
        else:
            out = config.data_dir / "exports" / f"飞盘讯息-{stamp}.jsonl"
            storage.export_jsonl(posts, out)

    ok(f"已导出 {len(posts)} 条到 {out}")
    return 0


# --------------------------------------------------------------------- stats


def do_stats(args: argparse.Namespace, config: AppConfig) -> int:
    db = resolve_db(config, args)
    if not db.exists():
        fail(f"数据库不存在：{db}\n先执行一次 python run.py crawl")
        return 2

    with Storage(db) as storage:
        header("本地库概况")
        stats = storage.stats()
        if stats.get("_total", 0) == 0:
            info("库里还没有数据。")
            return 1
        for platform, count in stats.items():
            if platform == "_total":
                continue
            info(f"  {platform_label(platform):<10} {count:>5} 条")
        info(f"  {'合计':<10} {stats['_total']:>5} 条")

        info("\n最近采集记录：")
        for row in storage.run_stats(limit=args.limit):
            ts = datetime.fromtimestamp(row["finished_ts"] or 0, tz=timezone.utc)
            local = ts.astimezone(timezone(timedelta(hours=8)))
            info(
                f"  {local.strftime('%m-%d %H:%M')}  "
                f"{platform_label(row['source']):<8} "
                f"{row['target'][:18]:<20} "
                f"抓取 {row['fetched']:>3} 保留 {row['kept']:>3} "
                f"新增 {row['inserted']:>3}  [{row['status']}]"
            )
    return 0


# --------------------------------------------------------------------- probe


async def do_probe(args: argparse.Namespace, config: AppConfig) -> int:
    """把某个平台真实请求到的接口和响应落盘，用于对齐字段。

    存在的理由：视频号没有公开的内容搜索接口，登录前连内容 API 都不触发，
    页面 JS 又是 blob 加载的、拿不到源码。所以「它的接口长什么样」这个
    问题只能靠登录后实测来回答 —— 这个命令就是干这个的。

    输出：data/probe/<平台>-<时间戳>.jsonl，每条一行 {url, 状态码, 结构摘要}；
    响应体完整落盘到同目录的 .payloads/ 下。
    """
    platform = args.platform
    headless = args.headless
    keyword = args.keyword or "飞盘"

    header(f"接口诊断：{platform_label(platform)}")

    if platform == "wechat_channels":
        base = "https://channels.weixin.qq.com"
        from .sources.wechat_channels import SEARCH_PATHS

        candidates = [base + p.format(q=quote(keyword)) for p in SEARCH_PATHS]
    elif args.url:
        candidates = [args.url]
    else:
        fail(f"{platform_label(platform)} 需要显式指定 --url，"
             "例如 --url 'https://www.xiaohongshu.com/search_result?keyword=飞盘'")
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = config.data_dir / "probe"
    payload_dir = out_dir / "payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []

    async with browser_context(
        platform, config.browser_state_dir, headless=headless,
        cdp_url=args.cdp or "",
    ) as context:
        if not await verify_login(context, platform):
            fail(f"{platform_label(platform)} 未登录，日志诊断不出来东西。先跑：\n"
                 f"    python run.py login --platform {platform}")
            return 1

        info(f"关键词：{keyword}")
        info(f"候选地址 {len(candidates)} 个，逐个打开并记录全部响应")
        info("")

        from .sources.wechat_channels import WechatChannelsSource

        page = await context.new_page()
        index = {"n": 0}

        async def on_response(response) -> None:
            url = response.url
            if any(url.endswith(e) for e in (".js", ".css", ".png", ".jpg", ".jpeg",
                                             ".gif", ".svg", ".woff", ".woff2", ".ico")):
                return
            content_type = (response.headers or {}).get("content-type", "")
            payload = None
            if "json" in content_type.lower():
                try:
                    payload = await response.json()
                except Exception:
                    payload = None

            index["n"] += 1
            record: dict = {
                "seq": index["n"],
                "url": url,
                "status": response.status,
                "content_type": content_type.split(";")[0],
            }
            if isinstance(payload, (dict, list)):
                record["top_keys"] = (
                    list(payload.keys())[:20] if isinstance(payload, dict) else f"list[{len(payload)}]"
                )
                # 用采集器同一套逻辑数一下能不能提出条目
                found: list[dict] = []
                WechatChannelsSource._find_cards(payload, found)
                if found:
                    record["cards_found"] = len(found)
                    record["card_keys"] = sorted(found[0].keys())[:20]
                    desc = found[0].get("objectDesc")
                    if isinstance(desc, dict):
                        record["objectDesc_keys"] = sorted(desc.keys())[:25]
                path = payload_dir / f"{stamp}-{index['n']:03d}.json"
                path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=1)[:400_000],
                    encoding="utf-8",
                )
                record["payload_file"] = path.name
            records.append(record)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        for url in candidates:
            info(f"--- 打开 {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=35_000)
            except Exception as exc:
                warn(f"    打开失败：{exc}")
                continue
            await asyncio.sleep(4)
            for _ in range(int(args.scrolls)):
                await page.mouse.wheel(0, 2200)
                await asyncio.sleep(2.0)
            text = " ".join((await page.inner_text("body")).split())
            info(f"    页面文本长度：{len(text)}"
                 + ("" if text else "（空 —— 通常意味着没登录成功或被拦）"))
            await asyncio.sleep(1)

        await page.close()

    out_path = out_dir / f"{platform}-{stamp}.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---- 汇总 ----
    info("")
    header("诊断结果")
    info(f"共记录 {len(records)} 个响应 → {out_path}")

    content_hits = [r for r in records if r.get("cards_found")]
    if content_hits:
        ok(f"发现 {len(content_hits)} 个能提出内容的接口：")
        for record in content_hits[:10]:
            info(f"    {record['url'][:100]}")
            info(f"       提出 {record['cards_found']} 条"
                 f"　字段：{', '.join(record.get('card_keys', [])[:8])}")
            if record.get("objectDesc_keys"):
                info(f"       objectDesc 字段：{', '.join(record['objectDesc_keys'][:12])}")
        info("")
        info("把这个文件和上面的字段名发我，就能把采集器的地址和字段对齐。")
    else:
        warn("没有任何接口能提出内容。可能的原因：")
        info("    · 登录态其实没生效（页面文本长度为 0 就是这种情况）")
        info("    · 搜索页路由变了 —— 看上面各候选地址的文本长度，非 0 的那个更接近")
        info("    · 内容是用非 JSON 方式传的（比如 protobuf），需要另做解析")
        info(f"原始响应都留在 {payload_dir}，可以据此判断")

    info("")
    info("响应清单（按序号）：")
    for record in records[:30]:
        mark = f" ← {record['cards_found']} 条" if record.get("cards_found") else ""
        info(f"    [{record['seq']:>3}] {record['status']} {record['url'][:88]}{mark}")
    if len(records) > 30:
        info(f"    …… 另 {len(records) - 30} 条见文件")

    return 0


# ------------------------------------------------------------------- publish


# 这些路径绝不能被提交到公开仓库 —— 里面是登录态和原始数据
GIT_FORBIDDEN_PREFIXES = ("data/", "data\\", ".wrangler/", ".wrangler\\")
GIT_FORBIDDEN_SUFFIXES = (".db", ".env", "cookies.json")
GIT_MUST_IGNORE = (
    "data/browser_state",
    "data/frisbee.db",
    "data/mock.db",
    "data/login_debug",
    "data/probe",
    # wrangler 的缓存里有 Cloudflare 账号 ID 和项目名。
    # 不含凭据，但不该公开，而且每次部署都会变。
    ".wrangler",
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )


def _assert_git_safe(root: Path) -> list[str]:
    """确认敏感文件进不了公开仓库。

    推之前必须过这一关：data/browser_state/ 里是各平台的登录 cookie，
    一旦提交到公开仓库就等于把账号会话公开了。宁可拦住不让推，
    也不要事后去删 —— 公开仓库的历史是会被抓取的。
    """
    problems: list[str] = []

    for rel in GIT_MUST_IGNORE:
        if not (root / rel).exists():
            continue
        result = _git(root, "check-ignore", "-q", rel)
        if result.returncode != 0:
            problems.append(
                f"{rel} 没有被 .gitignore 挡住（里面可能是登录态或原始数据）"
            )

    staged = _git(root, "diff", "--cached", "--name-only")
    for line in (staged.stdout or "").splitlines():
        name = line.strip()
        if not name:
            continue
        if name.startswith(GIT_FORBIDDEN_PREFIXES) or name.endswith(GIT_FORBIDDEN_SUFFIXES):
            problems.append(f"已暂存了不该提交的文件：{name}")

    return problems


def _pages_url(root: Path) -> str:
    """从 git remote 推出 GitHub Pages 网址。

    规则：https://<用户>.github.io/<仓库>/
    仓库名以 .github.io 结尾的话（用户主页仓库）网址就是 https://<用户>.github.io/
    """
    import re

    remote = _git(root, "remote", "get-url", "origin")
    url = (remote.stdout or "").strip()
    if not url:
        return ""
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", url)
    if not m:
        return ""
    owner, repo = m.group(1), m.group(2)
    if repo.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{repo}/"


def _deploy_cloudflare(project: str, out_dir: Path, root: Path) -> bool:
    """把静态站部署到 Cloudflare Pages。

    走 `npx wrangler pages deploy`（直传模式：本地把文件传上去），
    不是 Cloudflare 的「Git 集成」（那个要它自己去拉仓库，得在后台点一堆东西）。
    直传的好处是全程命令行，不用碰后台。

    只在这一步之前已经确认过「内容有变化」时才调用 ——
    没新内容就不该重新部署，否则会平白多一次部署记录。
    """
    info("")
    info(f"部署到 Cloudflare Pages（项目 {project}）…")
    try:
        result = subprocess.run(
            ["npx", "--yes", "wrangler@latest", "pages", "deploy", str(out_dir),
             "--project-name", project, "--branch", "main", "--commit-dirty=true"],
            cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600, shell=False,
        )
    except FileNotFoundError:
        warn("找不到 npx —— 需要 Node.js 才能部署 Cloudflare。")
        info("  浏览器里访问 https://feipannews.pages.dev/ 仍可正常看（上次部署的内容）")
        return False
    except subprocess.TimeoutExpired:
        warn("部署超时（超过 10 分钟），跳过。")
        return False

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        warn("Cloudflare 部署失败：")
        for line in output.strip().splitlines()[-6:]:
            info(f"    {line}")
        if "not authenticated" in output.lower() or "login" in output.lower():
            info("")
            info("看起来是没登录。跑一次：npx wrangler login")
            info("（会弹浏览器让你授权一次，之后长期有效）")
        info("")
        info("GitHub Pages 那份不受影响，仍然是好的。")
        return False

    ok("Cloudflare 部署完成")
    info(f"  网页地址：https://{project}.pages.dev/")
    info("  （1~2 分钟生效；Cloudflare 是直传，不需要等构建）")
    return True


def do_publish(args: argparse.Namespace, config: AppConfig) -> int:
    """把库里的内容生成静态网页；--push 时提交并推送到 GitHub。"""
    db = resolve_db(config, args)
    if not db.exists():
        fail(f"数据库不存在：{db}\n先跑一次 python run.py crawl")
        return 2

    header("生成网页")

    out_dir = Path(args.out) if args.out else config.publish.resolve_dir()

    with Storage(db) as storage:
        posts = storage.query(min_relevance=0.5, limit=20000)
        if not posts:
            warn("库里没有可发布的内容。")
            return 1
        info(f"从库里读到 {len(posts)} 条相关内容")
        site_info = generate_site(
            posts, out_dir, config.build_classifier(),
            title=config.publish.title,
            skip_if_unchanged=bool(args.if_changed),
        )

    if site_info.get("unchanged"):
        ok("内容和上次一致，保留原有网页（不重写文件、不刷新时间、不提交）")
        info(f"  当前收录 {site_info['total']} 条，内容最新到 {site_info['latestDate'] or '—'}")
        return 0

    ok(f"网页已生成：{out_dir}")
    info(f"  收录 {site_info['total']} 条　"
         f"分类：{'、'.join(f'{k} {v}' for k, v in site_info['categories'].items())}")
    info(f"  平台：{'、'.join(f'{k} {v}' for k, v in site_info['platforms'].items())}")
    if site_info["latestDate"]:
        info(f"  内容最新到 {site_info['latestDate']}")
    info("")
    info(f"本地预览：{out_dir / 'index.html'}")
    info("（直接双击本地文件时浏览器会拦截 data.json，想看效果需要起个本地服务器：")
    info(f"   cd {out_dir} && python -m http.server 8000　然后开 http://127.0.0.1:8000 ）")

    if not args.push:
        info("")
        info("想发布到 GitHub：加 --push（首次见 README 的「部署网页」一节）")
        return 0

    # ---------- 推送 ----------
    header("推送到 GitHub")

    root = PROJECT_ROOT
    if _git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        fail("这个目录还不是 git 仓库。先执行：")
        info(f"   cd {root}")
        info("   git init -b main")
        info("   git add . && git commit -m '飞盘讯息聚合：首次提交'")
        info("   git remote add origin https://github.com/<你的用户名>/<仓库名>.git")
        info("   git push -u origin main")
        info("（或者在 README 的「部署网页」一节里用 gh 一条命令建仓）")
        return 1

    problems = _assert_git_safe(root)
    if problems:
        fail("安全检查没通过，已停止推送：")
        for p in problems:
            info(f"   · {p}")
        info("")
        info("确认 .gitignore 里有 data/ 之后重试。登录态一旦推到公开仓库，")
        info("就算之后删掉，历史里仍然能翻出来。")
        return 1

    rel = out_dir.relative_to(root) if out_dir.is_relative_to(root) else out_dir
    _git(root, "add", str(rel))
    staged = _git(root, "diff", "--cached", "--name-only")
    content_changed = bool((staged.stdout or "").strip())

    if not content_changed:
        info("网页内容没有变化，跳过提交。")
    else:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        commit = _git(root, "commit", "-m", f"更新网页内容 {stamp}")
        if commit.returncode != 0:
            fail(f"提交失败：{commit.stderr.strip()[:200]}")
            return 1
        ok("已提交")

        push = _git(root, "push", "origin", config.publish.branch)
        if push.returncode != 0:
            fail(f"推送失败：{(push.stderr or push.stdout).strip()[:300]}")
            info("如果是首次推送，确认远端已配置：git remote -v")
            return 1
        ok(f"已推送到 origin/{config.publish.branch}")

    # Cloudflare Pages：只在内容真变了才重新部署。
    # 跟上面「没变化就跳过提交」同一个原则 —— 没新东西就不该产生部署记录。
    cf_project = config.publish.cloudflare_project
    if cf_project and content_changed:
        _deploy_cloudflare(cf_project, out_dir, root)
    elif cf_project and not content_changed:
        info("（内容没变，Cloudflare 也不用重新部署）")

    info("")
    url = _pages_url(root)
    if url:
        info(f"网页地址：{url}")
        info("（GitHub Pages 需要 1~2 分钟构建，稍等再刷新）")
    else:
        info("GitHub Pages 会在 1~2 分钟内更新，网址形如：")
        info("   https://<用户名>.github.io/<仓库名>/")
    info("若显示 404：去仓库 Settings → Pages，把 Source 选成 main 分支的 /docs（只需设置一次）")
    return 0


# -------------------------------------------------------------------- domain


# GitHub Pages 的官方 A 记录（主域名必须用 A 记录，不能用 CNAME）
GITHUB_PAGES_IPS = {
    "185.199.108.153",
    "185.199.109.153",
    "185.199.110.153",
    "185.199.111.153",
}

# 域名的形状校验。必须先校验再交给 socket ——
# 实测 socket.getaddrinfo("") 不报错，而是返回**本机**的地址
# （172.x / 192.168.x / fe80::…），于是"DNS 查得到"这个判断会被误判成通过。
_DOMAIN_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$",
    re.IGNORECASE,
)


def _resolve_domain(domain: str) -> tuple[list[str], str]:
    """查域名的解析结果，返回 (地址列表, CNAME 目标)。

    只用标准库，不额外引依赖。**这是唯一碰网络的地方**，
    判据逻辑放在 _domain_ready_for_pages 里，方便离线测试。
    """
    import socket

    domain = (domain or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(domain):
        return [], ""

    addresses: list[str] = []
    try:
        infos = socket.getaddrinfo(domain, None)
        addresses = sorted({i[4][0] for i in infos})
    except Exception:
        return [], ""

    cname = ""
    try:
        result = subprocess.run(
            ["nslookup", "-type=CNAME", domain],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        for line in (result.stdout or "").splitlines():
            low = line.lower()
            if "canonical name" in low or "别名" in line:
                target = line.split("=")[-1].strip().rstrip(".")
                if target:
                    cname = target.lower()
                    break
    except Exception:
        pass

    return addresses, cname


def _domain_ready_for_pages(
    domain: str, owner: str, *, resolve=None
) -> tuple[bool, str, str | None]:
    """判断域名是否已经正确指向 GitHub Pages。

    返回 (是否就绪, 给人看的说明, 错误提示)。

    ⚠️ 判据必须是「指向 GitHub Pages」，而不是「能解析出东西」。
    实测这台机器的 DNS 在做**域名劫持** —— 路由器对任何不存在的域名
    （连 nonexistent.invalid 这种保留域名）都返回一个 IP。如果只检查
    "能不能解析"，那么在劫持 DNS 下任意垃圾域名都会通过检查，
    然后被当成有效域名设上去 —— 原 github.io 会 301 跳到那个打不开的
    域名，**网站直接下线**。这正是这道护栏要防的事，所以判据得抗劫持：
    劫持返回的不可能是 `指向 <owner>.github.io 的 CNAME`，也不会是
    GitHub Pages 那四个官方 IP。

    根域名不接受 CNAME，得配那 4 条 A 记录；子域一般配 CNAME 到
    `<owner>.github.io`。两种都认。
    """
    resolve = resolve or _resolve_domain
    domain = (domain or "").strip().lower().rstrip(".")
    target = f"{owner}.github.io"

    if not domain:
        return False, "没给域名", "域名是空的"
    if not _DOMAIN_RE.match(domain):
        return False, f"{domain} 不像一个合法域名", "格式不对"

    addresses, cname = resolve(domain)

    if cname and cname.rstrip(".").endswith(target):
        return True, f"{domain} → CNAME → {cname}", None

    pages_ips = sorted(set(addresses) & GITHUB_PAGES_IPS)
    if pages_ips:
        return True, f"{domain} → A → {', '.join(pages_ips)}（GitHub Pages 官方 IP）", None

    if addresses or cname:
        detail = []
        if cname:
            detail.append(f"CNAME 指向 {cname}")
        if addresses:
            detail.append(f"解析到 {', '.join(addresses[:3])}")
        return (
            False,
            f"{domain} 能解析（{'；'.join(detail)}），但**没指向 GitHub Pages**",
            None,
        )

    return False, f"{domain} 解析不出任何地址", None


def do_domain(args: argparse.Namespace, config: AppConfig) -> int:
    """给静态站绑自定义域名（或解绑）。

    ⚠️ 顺序很重要，搞反会让网站暂时打不开：
       1. **先在域名商那里加 DNS 记录**（CNAME 指向 <用户名>.github.io）
       2. 等它生效（几分钟到几小时）
       3. 再运行本命令把域名告诉 GitHub Pages

    一旦 GitHub Pages 认了自定义域名，原来的 <用户名>.github.io/<仓库>/
    会自动 301 跳到新域名 —— 所以 DNS 没生效就设置，等于把两个地址
    都弄成打不开。
    """
    repo = args.repo or "realchenchenluo/frisbee-radar"
    out_dir = config.publish.resolve_dir()

    if args.clear:
        header("解绑自定义域名")
        result = subprocess.run(
            ["gh", "api", "-X", "DELETE", f"repos/{repo}/pages", "-f", "cname="],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        cname_file = out_dir / "CNAME"
        if cname_file.exists():
            cname_file.unlink()
            info(f"已删除 {cname_file}")
        if result.returncode == 0:
            ok("已解绑，网站回到默认地址")
            info(f"  https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/")
        else:
            warn(f"解绑可能没成功：{(result.stderr or result.stdout).strip()[:200]}")
            info("也可以去仓库 Settings → Pages 手动清除 Custom domain")
        return 0

    domain = (args.domain or "").strip()
    if not domain:
        fail("要指定域名：python run.py domain feipan.info")
        info("解绑用：python run.py domain --clear")
        return 2

    header(f"绑定自定义域名：{domain}")

    # ---- 先确认域名真的指向 GitHub Pages，没配好就别继续 ----
    owner = repo.split("/")[0]
    info(f"正在检查 {domain} 是否已指向 GitHub Pages…")
    ready, detail, _ = _domain_ready_for_pages(domain, owner)
    info(f"  {detail}")

    if not ready:
        fail("DNS 还没配好，已停止 —— 现在设置会让网站直接下线。")
        info("")
        info("  1. 去你的域名商（阿里云/腾讯云/Cloudflare/Namecheap…）的 DNS 设置")
        info("  2. 加记录：")
        info("       根域名（feipan.info 这种）：")
        info("         类型 A，主机记录 @，记录值依次填这 4 个：")
        for ip in sorted(GITHUB_PAGES_IPS):
            info(f"           {ip}")
        info("       子域名（www.feipan.info 这种）：")
        info("         类型 CNAME，主机记录 www，记录值 " + f"{owner}.github.io")
        info("  3. 等几分钟到几小时生效，再重跑本命令")
        info("")
        info("为什么不能跳过：GitHub Pages 一旦认了自定义域名，原来的 github.io")
        info("地址会 301 跳到新域名 —— DNS 没生效就设，两个地址都会打不开。")
        info("")
        info("（提示：判断依据是「有没有指向 GitHub Pages」，不是「能不能解析」。")
        info("  有些路由器会对不存在的域名返回一个 IP 做劫持，只看能不能解析会误判。）")
        return 1

    ok(f"DNS 就绪：{detail}")

    # ---- 写 CNAME 文件并推送（GitHub Pages 认这个文件）----
    root = PROJECT_ROOT
    cname_file = out_dir / "CNAME"
    cname_file.write_text(domain + "\n", encoding="utf-8")
    info(f"已写入 {cname_file}")

    if _git(root, "rev-parse", "--is-inside-work-tree").returncode == 0:
        try:
            rel_path = cname_file.relative_to(root)
        except ValueError:
            # 输出目录被改到仓库外了（测试或自定义配置），那就只写文件不提交
            warn("输出目录不在仓库里，CNAME 不会提交到 git —— 手动把它放到仓库内再推。")
            rel_path = None
        if rel_path is not None:
            _git(root, "add", str(rel_path))
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        _git(root, "commit", "-m", f"绑定自定义域名 {domain}（{stamp}）")
        push = _git(root, "push", "origin", config.publish.branch)
        if push.returncode == 0:
            ok("CNAME 已推送")
        else:
            warn(f"推送失败：{(push.stderr or push.stdout).strip()[:200]}")

    # ---- 在 GitHub Pages 上登记域名 ----
    result = subprocess.run(
        ["gh", "api", "-X", "PUT", f"repos/{repo}/pages",
         "-f", f"cname={domain}", "-f", "https_enforced=true"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        warn("通过 API 登记域名没成功，去仓库 Settings → Pages 手动填 Custom domain：")
        info(f"  https://github.com/{repo}/settings/pages")
        info(f"  Custom domain 填：{domain}")
    else:
        ok("已在 GitHub Pages 登记")

    info("")
    info("接下来：")
    info("  1. 等 1~2 分钟，GitHub 会自动签发 HTTPS 证书（首次可能十几分钟）")
    info(f"  2. 打开 https://{domain}/ 确认")
    info("  3. 在 Settings → Pages 勾上 Enforce HTTPS（如果还没勾）")
    info("")
    info("原来的地址会自动 301 跳到新域名，不用改任何别的东西 ——")
    info("每天 21:00 的定时任务照常往 GitHub 推，新域名自动跟着更新。")
    return 0


# --------------------------------------------------------------------- daily


async def do_daily(args: argparse.Namespace, config: AppConfig) -> int:
    """定时任务的入口：采集 → 判断 → 出日报 → 更新网页 → 推送。

    **核心原则（用户明确要求）：没有新内容就什么都不做。**

      · 不写日报文件
      · 不重写网页，不刷新「最近更新」时间
      · 不产生 git 提交
      · 绝不为了让页面"看起来有更新"而改动任何内容 ——
        网页和日报里出现的每一条都来自实际采集，采集不到就是没有。

    所以这个命令跑完可能是「什么都没做」，那是正常结果，日志里会写明原因。
    """
    started = datetime.now()
    header(f"每日自动更新　{started.strftime('%Y-%m-%d %H:%M')}")

    # 1) 采集 + 判断要不要出日报（复用 watch 的全部逻辑）
    watch_args = argparse.Namespace(
        source=args.source,
        no_crawl=args.no_crawl,
        force=args.force,
        min_relevance=args.min_relevance,
        output=None,
        db=args.db,
    )
    crawl_code = await do_watch(watch_args, config)

    # 2) 更新网页。用 --if-changed：内容和上次一致就一个字节都不写。
    #    注意这一步和"要不要出日报"是独立的 —— 就算没达到出日报的阈值，
    #    只要库里有新内容，网页也该更新。
    info("")
    publish_args = argparse.Namespace(
        out=None, push=args.push, if_changed=True, db=args.db,
    )
    publish_code = do_publish(publish_args, config)

    elapsed = int((datetime.now() - started).total_seconds())
    header(f"本次结束（耗时 {elapsed} 秒）")
    return crawl_code or publish_code


# ------------------------------------------------------------------- targets


def do_targets(args: argparse.Namespace, config: AppConfig) -> int:
    header("当前配置的采集目标")
    for name in ("wechat", "xiaohongshu", "douyin"):
        settings = config.platform(name)
        state = "启用" if settings.enabled else "禁用"
        mode = f" mode={settings.mode}" if settings.mode else ""
        info(f"\n[{state}] {platform_label(name)}{mode}")
        if settings.keywords:
            info("  关键词：" + "、".join(settings.keywords))
        if settings.accounts:
            info("  账号：" + "、".join(settings.accounts))
        if not settings.keywords and not settings.accounts:
            warn("  没有配置任何目标")
    info("\n以上内容在 config/sources.yaml 里修改。")
    return 0


# ------------------------------------------------------------------- authors


def do_authors(args: argparse.Namespace, config: AppConfig) -> int:
    db = resolve_db(config, args)
    if not db.exists():
        fail(f"数据库不存在：{db}")
        return 2

    auth = {a.strip() for a in config.watch.authoritative if a.strip()}
    with Storage(db) as storage:
        rows = storage.author_counts(limit=args.limit)
        if not rows:
            warn("库里还没有带作者的记录。")
            return 1

        header("已采到的账号（按条数排序）")
        hit = 0
        for author, count in rows:
            marks = []
            if author in auth:
                marks.append("权威号")
                hit += 1
            elif auth and any(a in author or author in a for a in auth):
                marks.append("疑似权威号(名字不完全一致)")
            suffix = f"  ← {'、'.join(marks)}" if marks else ""
            info(f"  {count:>4}  {author}{suffix}")

        info("")
        info(f"共 {len(rows)} 个账号。config/sources.yaml 里配了 {len(auth)} 个权威号。")
        if auth and hit == 0:
            warn("配置的权威号一个都没匹配上——名字必须和上面的 author 完全一致。")
            warn("另外 sogou 模式只抓得到被搜狗收录的号，可能它根本没出现过。")
    return 0


# ------------------------------------------------------------------ baseline


def do_baseline(args: argparse.Namespace, config: AppConfig) -> int:
    """把现有内容全部标记为「已报送」，立一个增量起点。"""
    db = resolve_db(config, args)
    if not db.exists():
        fail(f"数据库不存在：{db}")
        return 2

    with Storage(db) as storage:
        total = storage.stats().get("_total", 0)
        pending = storage.count_unreported(min_relevance=0.0)
        if not pending:
            info("没有未报送的内容，无需建立基线。")
            return 0

        if not args.yes:
            warn(f"即将把 {pending} 条现有内容全部标记为「已报送」。")
            warn("这只影响增量判断，库里的数据一条都不会删。")
            warn("之后 watch 只会对「从此刻起新采到」的内容出简报。")
            info("确认请加 --yes 重新执行：python run.py baseline --yes")
            return 1

        count = storage.mark_all_reported_before()
        ok(f"已建立基线：{count} 条标记为已报送（库内共 {total} 条）。")
        info("下次 watch 只会针对新采到的内容决定是否出简报。")
    return 0


# --------------------------------------------------------------------- watch


async def do_watch(args: argparse.Namespace, config: AppConfig) -> int:
    """采集一轮，然后判断「值不值得出一份日报」。

    产出的判定逻辑（三条任一成立就出）：
      1. --force
      2. 权威公众号有新内容（可选：无视最短间隔）
      3. 可收录的新内容累计到 trigger_min_new

    注意阈值数的是**可收录**条数：命中「训练与技巧」或「队伍与赛事动态」
    的内容才计数。不属于任何分类的内容不计数、不进日报，见 categories.py。

    每条内容只会进一份日报——出完就打 reported_ts 标记。
    """
    db = resolve_db(config, args)
    watch_cfg = config.watch
    min_relevance = args.min_relevance
    classifier = config.build_classifier()
    auth_set = {a.strip() for a in watch_cfg.authoritative if a.strip()}

    header("飞盘日报 watch" + ("（跳过采集，只看存量）" if args.no_crawl else ""))

    # ---- 1. 采集 ----
    crawl_code = 0
    if not args.no_crawl:
        crawl_args = argparse.Namespace(
            source=args.source, only=None, dry_run=False, verbose=False,
            mock=False, mock_as=None,
        )
        crawl_code = await do_crawl(crawl_args, config)
        if crawl_code != 0:
            warn("采集过程有报错，继续用库里已有的内容判断。")

    # ---- 2. 判断该不该产出 ----
    now = datetime.now(timezone.utc)
    interval = timedelta(hours=watch_cfg.min_interval_hours)

    with Storage(db) as storage:
        # 多取一些：要从中挑出可收录的，未归类的那些会被丢掉
        pending = storage.unreported(
            min_relevance=min_relevance, limit=max(config.output.max_items * 4, 200)
        )
        grouped, excluded = classifier.split(pending)
        included = [p for items in grouped.values() for p in items]
        auth_new = [p for p in included if p.author in auth_set]

        last_report = storage.last_reported_at()
        interval_ok = last_report is None or (now - last_report) >= interval

        info("")
        info(f"未报送 {len(pending)} 条 → 可收录 {len(included)} 条"
             + (f"（权威号 {len(auth_new)} 条）" if auth_set else ""))
        if excluded:
            info(f"另有 {len(excluded)} 条不属于任何分类，本次不计入"
                 "（媒体评论、行业分析、装备种草等）")
        for key, items in grouped.items():
            name = next(
                (c.name for c in classifier.categories if c.key == key), key
            )
            latest = max(
                (p.publish_time for p in items if p.publish_time), default=None
            )
            info(f"    · {name}：{len(items)} 条，最新 {_fmt_date_short(latest)}")
        if last_report is not None:
            local = last_report.astimezone(timezone(timedelta(hours=8)))
            info(f"上一份日报：{local.strftime('%Y-%m-%d %H:%M')}")

        should, reason = False, ""
        if args.force:
            should, reason = True, "指定了 --force"
        elif auth_new and (interval_ok or watch_cfg.authoritative_breaks_interval):
            should, reason = True, f"权威号有新推送（{len(auth_new)} 条）"
        elif len(included) >= watch_cfg.trigger_min_new and interval_ok:
            should, reason = True, (
                f"可收录 {len(included)} 条，达到阈值 {watch_cfg.trigger_min_new} 条"
            )

        if not should:
            info("")
            if not included:
                ok("没有可收录的新内容，本次不出日报。")
            elif not interval_ok and len(included) >= watch_cfg.trigger_min_new:
                remain = int((interval - (now - last_report)).total_seconds() // 60)  # type: ignore[operator]
                ok(f"可收录 {len(included)} 条，但距上次日报不足 "
                   f"{watch_cfg.min_interval_hours:g} 小时（还差约 {remain} 分钟），暂不出。")
            else:
                ok(f"可收录 {len(included)} 条，未达阈值 "
                   f"{watch_cfg.trigger_min_new} 条，暂不出日报。")
            info("想强制出一份：python run.py watch --no-crawl --force")
            return crawl_code

        # ---- 3. 生成日报 ----
        posts = included[: config.output.max_items]
        truncated = len(included) - len(posts)

        info("")
        ok(f"触发产出：{reason}")

        out_dir = config.output.resolve_dir()
        if args.output:
            out = Path(args.output)
        else:
            out = out_dir / config.output.resolve_filename()

        build_docx_daily(
            posts,
            out,
            title=config.output.title,
            classifier=classifier,
            authoritative=watch_cfg.authoritative,
            # 把全局口径传进去，数据说明才能写明「扫了多少、剔了多少」
            scanned_total=len(pending),
            excluded_total=len(excluded),
        )

        # 原始数据落一份，便于回溯；沿用 X 那套 {date}_raw.json 的习惯
        if config.output.also_json:
            raw_path = out.with_name(out.stem + "_原始数据.json")
            storage.export_jsonl(posts, raw_path.with_suffix(".jsonl"))

        # 收录的标记为已报送；未归类的也一并标记为已处理，
        # 否则它们会永远留在未报送状态、每次都把 watch 判成「有新内容」
        marked = storage.mark_reported(posts)
        if excluded:
            storage.mark_reported(excluded[:500])

        ok(f"日报已生成：{out}")
        info(
            f"收录 {len(posts)} 条"
            + (f"（权威号 {sum(1 for p in posts if p.author in auth_set)} 条）" if auth_set else "")
            + f"，已标记 {marked} 条为已报送。"
        )
        if truncated > 0:
            warn(f"还有 {truncated} 条可收录内容，会出现在下一份日报里。")

    return crawl_code


def _fmt_date_short(dt: datetime | None) -> str:
    if dt is None:
        return "时间未知"
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")



# ---------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="飞盘讯息抓取 —— 微信公众号 / 小红书 / 抖音 飞盘内容聚合",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="扫码登录并保存登录态")
    p_login.add_argument("--platform", required=True, choices=list(LOGIN_URLS))
    p_login.add_argument("--timeout", type=int, default=180, help="等待扫码的秒数")
    p_login.add_argument(
        "--cdp",
        help="连到你自己的浏览器（如 http://127.0.0.1:9222）而不是开独立的，"
             "此时复用你浏览器里已有的登录态。配合「7-用我的浏览器.bat」使用",
    )

    p_crawl = sub.add_parser("crawl", help="执行采集")
    p_crawl.add_argument("--source", choices=AVAILABLE, help="只跑某个平台")
    p_crawl.add_argument("--only", help="只跑某个关键词或账号")
    p_crawl.add_argument("--dry-run", action="store_true", help="只打印，不写数据库")
    p_crawl.add_argument("--verbose", "-v", action="store_true", help="打印被过滤掉的条目")
    p_crawl.add_argument("--mock", action="store_true", help="用离线样例数据跑通流程")
    p_crawl.add_argument("--mock-as", choices=["wechat", "xiaohongshu", "douyin"],
                         help="配合 --mock，指定模拟哪个平台")

    p_report = sub.add_parser("report", help="生成 Markdown 简报")
    p_report.add_argument("--days", type=int, default=7, help="统计最近几天")
    p_report.add_argument("--platform", choices=["wechat", "xiaohongshu", "douyin"])
    p_report.add_argument("--top", type=int, default=10, help="热度榜条数")
    p_report.add_argument("--limit", type=int, default=500)
    p_report.add_argument("--min-relevance", type=float, default=0.5)
    p_report.add_argument("--output", help="输出路径")
    p_report.add_argument("--title", help="自定义报告标题")
    p_report.add_argument("--db", help="指定数据库文件（默认 data/frisbee.db；"
                                       "用 --mock 跑时可指 data/mock.db）")

    p_export = sub.add_parser("export", help="导出数据")
    p_export.add_argument("--format", choices=["csv", "jsonl"], default="csv")
    p_export.add_argument("--days", type=int, default=30)
    p_export.add_argument("--limit", type=int, default=5000)
    p_export.add_argument("--min-relevance", type=float, default=0.5)
    p_export.add_argument("--db", help="指定数据库文件")

    p_stats = sub.add_parser("stats", help="查看本地库统计")
    p_stats.add_argument("--limit", type=int, default=15)
    p_stats.add_argument("--db", help="指定数据库文件")

    sub.add_parser("targets", help="列出当前配置的采集目标")

    p_authors = sub.add_parser("authors", help="列出已采到的账号（用来配权威号）")
    p_authors.add_argument("--limit", type=int, default=60)
    p_authors.add_argument("--db", help="指定数据库文件")

    p_probe = sub.add_parser(
        "probe",
        help="把某平台真实请求到的接口和响应落盘（对齐字段用）",
        description="打开页面并记录全部响应，指出哪个接口能提出内容、字段叫什么。"
                    "视频号没有公开文档，靠这个把接口对齐。",
    )
    p_probe.add_argument("--platform", required=True, choices=list(LOGIN_URLS))
    p_probe.add_argument("--keyword", default="飞盘", help="搜索关键词")
    p_probe.add_argument("--url", help="直接指定要打开的地址（非视频号平台需要）")
    p_probe.add_argument("--scrolls", type=int, default=4, help="滚动几轮触发加载")
    p_probe.add_argument("--headless", action="store_true",
                         help="不显示浏览器窗口（默认显示，便于看是否卡在登录）")
    p_probe.add_argument("--cdp", help="连到你自己的浏览器（如 http://127.0.0.1:9222）")

    p_baseline = sub.add_parser(
        "baseline", help="把现有内容标记为已报送，建立增量起点"
    )
    p_baseline.add_argument("--yes", action="store_true", help="确认执行")
    p_baseline.add_argument("--db", help="指定数据库文件")

    p_watch = sub.add_parser(
        "watch",
        help="采集一次，只在出现新内容时生成 DOCX 简报",
        description="增量模式：采集 → 判断有无新内容 → 有则出 DOCX 简报。"
                    "每条内容只会进一份简报。",
    )
    p_watch.add_argument("--source", choices=AVAILABLE, help="只跑某个平台")
    p_watch.add_argument("--no-crawl", action="store_true",
                         help="跳过采集，只对库里存量做判断")
    p_watch.add_argument("--force", action="store_true",
                         help="无视阈值和最短间隔，立刻出简报")
    p_watch.add_argument("--min-relevance", type=float, default=0.5)
    p_watch.add_argument("--output", help="指定输出路径")
    p_watch.add_argument("--db", help="指定数据库文件")

    p_publish = sub.add_parser(
        "publish",
        help="生成静态网页（--push 则提交并推送到 GitHub）",
        description="把库里的内容生成成静态网页。GitHub Pages 只能托管静态文件，"
                    "抓取必须在本地跑，所以流程是「本地生成 → 推送 → Pages 托管」。",
    )
    p_publish.add_argument("--out", help="输出目录（默认取配置的 publish.dir）")
    p_publish.add_argument("--push", action="store_true",
                          help="提交并推送到 GitHub（推送前会做敏感文件检查）")
    p_publish.add_argument("--if-changed", action="store_true",
                          help="内容没变就什么都不做（定时任务用这个，"
                               "避免没新内容也刷新一遍）")
    p_publish.add_argument("--db", help="指定数据库文件")

    p_daily = sub.add_parser(
        "daily",
        help="每日自动更新：采集 → 判断 → 出日报 → 更新网页 → 推送",
        description="定时任务调用的就是这条。原则：**没有新内容就什么都不做** ——"
                    "不写日报、不重写网页、不刷新时间戳、不产生提交。",
    )
    p_daily.add_argument("--push", action="store_true", help="更新后推送到 GitHub")
    p_daily.add_argument("--force", action="store_true",
                         help="无视阈值，强制出一份日报（网页仍只在内容变化时更新）")
    p_daily.add_argument("--source", choices=AVAILABLE, help="只跑某个平台")
    p_daily.add_argument("--no-crawl", action="store_true",
                         help="跳过采集，只按库里存量处理")
    p_daily.add_argument("--min-relevance", type=float, default=0.5)
    p_daily.add_argument("--db", help="指定数据库文件")

    p_domain = sub.add_parser(
        "domain",
        help="给网页绑自定义域名（换掉 github.io 那个地址）",
        description="把静态站绑到自己的域名上。注意顺序：先在域名商加 DNS 记录，"
                    "等生效了再跑这个命令 —— 反过来会让网站暂时打不开。",
    )
    p_domain.add_argument("domain", nargs="?", help="要绑的域名，如 feipan.info")
    p_domain.add_argument("--clear", action="store_true", help="解绑，回到 github.io 地址")
    p_domain.add_argument("--repo", help="GitHub 仓库，默认 realchenchenluo/frisbee-radar")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config()

    if args.command == "login":
        return asyncio.run(
            do_login(args.platform, config, args.timeout, cdp=args.cdp or "")
        )
    if args.command == "crawl":
        try:
            return asyncio.run(do_crawl(args, config))
        except KeyboardInterrupt:
            warn("已中断。")
            return 130
    if args.command == "report":
        return do_report(args, config)
    if args.command == "export":
        return do_export(args, config)
    if args.command == "stats":
        return do_stats(args, config)
    if args.command == "targets":
        return do_targets(args, config)
    if args.command == "authors":
        return do_authors(args, config)
    if args.command == "baseline":
        return do_baseline(args, config)
    if args.command == "probe":
        try:
            return asyncio.run(do_probe(args, config))
        except KeyboardInterrupt:
            warn("已中断。")
            return 130
    if args.command == "publish":
        return do_publish(args, config)
    if args.command == "domain":
        return do_domain(args, config)
    if args.command == "daily":
        try:
            return asyncio.run(do_daily(args, config))
        except KeyboardInterrupt:
            warn("已中断。")
            return 130
    if args.command == "watch":
        try:
            return asyncio.run(do_watch(args, config))
        except KeyboardInterrupt:
            warn("已中断。")
            return 130

    parser.print_help()
    return 2
