"""静态网页生成。

## 为什么是静态站，而不是一个后端服务

用户想要的「网页一直存在」，最省事也最稳的答案是 GitHub Pages。
但 Pages **只能托管静态文件** —— 跑不了 Python，更跑不了浏览器。

而这套东西的抓取必须在本地跑，因为：

  1. **要浏览器内核**。小红书/抖音的接口都要页面自己算的签名参数
     （x-s / a_bogus），不是纯 HTTP 请求能构造的。
  2. **要登录态**。小红书搜索必须登录，登录态是浏览器 profile 里的 cookie。
  3. **机房 IP 会被拦**。GitHub Actions 的 runner 是数据中心 IP，
     小红书/抖音/搜狗对这类 IP 拦得很凶，定时任务基本跑不通。

所以架构是「本地抓 → 生成静态站 → 推 GitHub → Pages 永久托管」：

    你的电脑                                  GitHub
    ┌──────────────────────┐                 ┌────────────────────┐
    │ 1-生成日报.bat        │                 │ 仓库 docs/          │
    │  采集 → SQLite       │   git push      │   index.html       │
    │ 8-发布网页.bat        │ ──────────────► │   data.json        │
    │  生成静态站 → 推送     │                 └────────┬───────────┘
    └──────────────────────┘                          │
                                             GitHub Pages（永久、免费、免登录）
                                             https://<用户名>.github.io/<仓库>/

网页本身永远在线；内容靠你双击更新。这跟「每天自动抓」的区别是：
更新频率取决于你什么时候跑，而不是站点在不在。

## 关于发布什么内容

**只发布标题、作者、时间、互动数、链接和一小段摘要**，不发布全文。

理由有两层：
  · 版权：把别人公众号文章的正文整篇公开转载，是有风险的；
    只给标题 + 摘要 + 回链，属于常见的聚合做法，也给原作者带流量。
  · 体积：data.json 要控制大小，全文会让首屏加载变慢。

摘要截断到 EXCERPT_LIMIT 个字符，且明确标注来源和「点开原文」。
"""

from __future__ import annotations

import html
import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .categories import CategoryClassifier
from .models import Post, platform_label

# 摘要截断长度。只发布摘要不发布全文，理由见模块 docstring。
EXCERPT_LIMIT = 80

# 站点配色。跟 DOCX 日报用的是同一套（FG-1 Forest Mint），保持视觉一致。
BRAND = "#0C1F1A"
ACCENT = "#3DDBB5"
ACCENT_DARK = "#2A7A65"


def _excerpt(text: str, limit: int = EXCERPT_LIMIT) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone(timedelta(hours=8))).isoformat()


def build_payload(
    posts: list[Post],
    classifier: CategoryClassifier,
    *,
    generated_at: datetime | None = None,
) -> dict:
    """把内容整理成前端要用的 JSON。

    分类在这里算一次就固化进 JSON —— 前端不用重复实现一遍分类逻辑。
    """
    now = generated_at or datetime.now(timezone.utc)
    grouped, excluded = classifier.split([p for p in posts if p.relevance > 0])
    category_names = {c.key: c.name for c in classifier.categories}
    # 分类顺序按 order，前端按这个顺序渲染标签页
    order = [c.key for c in sorted(classifier.categories, key=lambda c: c.order)]

    items: list[dict] = []
    for key in order:
        for post in grouped.get(key, []):
            value, metric_label = (
                (post.views, "阅读") if post.platform == "wechat" else (post.likes, "点赞")
            )
            items.append({
                "title": post.title,
                "author": post.author,
                "platform": post.platform,
                "platformLabel": platform_label(post.platform),
                "category": key,
                "categoryLabel": category_names.get(key, key),
                "date": post.publish_time.strftime("%Y-%m-%d") if post.publish_time else "",
                "iso": _iso(post.publish_time),
                "excerpt": _excerpt(post.content if post.content != post.title else ""),
                "url": post.url,
                "metric": value,
                "metricLabel": metric_label,
                "relevance": round(post.relevance, 2),
            })

    # 时间倒序（无时间的排最后），同时间的按热度
    items.sort(key=lambda i: (i["iso"] or "", i["metric"]), reverse=True)

    platforms: dict[str, int] = {}
    for item in items:
        platforms[item["platformLabel"]] = platforms.get(item["platformLabel"], 0) + 1

    dates = [i["iso"] for i in items if i["iso"]]
    return {
        "generatedAt": now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "generatedDate": now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "total": len(items),
        "scanned": len(posts),
        "excluded": len(excluded),
        "platforms": platforms,
        "categories": [
            {"key": k, "label": category_names.get(k, k), "count": len(grouped.get(k, []))}
            for k in order
        ],
        "latestDate": (max(dates)[:10] if dates else ""),
        "earliestDate": (min(dates)[:10] if dates else ""),
        "items": items,
    }


# ------------------------------------------------------------------ HTML 模板

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<style>
:root {{
  --brand: {brand};
  --accent: {accent};
  --accent-dark: {accent_dark};
  --ink: #1a1f1d;
  --ink-soft: #5a6570;
  --ink-faint: #8a949e;
  --line: #e6eae8;
  --bg: #f7f8f7;
  --card: #ffffff;
}}
* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei",
               "Hiragino Sans GB", "Source Han Sans SC", sans-serif;
  font-size: 15px; line-height: 1.65;
}}
a {{ color: var(--accent-dark); }}

header.site {{
  background: var(--brand); color: #fff; padding: 26px 20px 22px;
}}
.wrap {{ max-width: 860px; margin: 0 auto; }}
header.site h1 {{ margin: 0 0 6px; font-size: 24px; letter-spacing: .5px; }}
header.site .sub {{ color: #b0b8c0; font-size: 13px; }}
header.site .stats {{ margin-top: 12px; color: #d6ded9; font-size: 13px; }}
header.site .stats b {{ color: var(--accent); font-weight: 600; }}
.rule {{ height: 3px; background: var(--accent); }}

nav.filters {{
  position: sticky; top: 0; z-index: 10; background: rgba(247,248,247,.96);
  backdrop-filter: blur(6px); border-bottom: 1px solid var(--line);
  padding: 12px 20px;
}}
.filters .row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.filters .row + .row {{ margin-top: 8px; }}
.chip {{
  border: 1px solid var(--line); background: #fff; color: var(--ink-soft);
  border-radius: 999px; padding: 5px 13px; font-size: 13px; cursor: pointer;
  font-family: inherit; transition: all .15s;
}}
.chip:hover {{ border-color: var(--accent-dark); color: var(--accent-dark); }}
.chip[aria-pressed="true"] {{
  background: var(--accent-dark); border-color: var(--accent-dark); color: #fff;
}}
.chip .n {{ opacity: .65; margin-left: 4px; font-size: 12px; }}
input.search {{
  flex: 1 1 190px; min-width: 150px; padding: 7px 13px; font-size: 14px;
  border: 1px solid var(--line); border-radius: 999px; font-family: inherit;
  background: #fff; color: var(--ink);
}}
input.search:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; }}

main {{ padding: 18px 20px 60px; }}
.card {{
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 15px 17px; margin-bottom: 11px; transition: box-shadow .15s;
}}
.card:hover {{ box-shadow: 0 2px 12px rgba(12,31,26,.07); }}
.card h2 {{ margin: 0 0 7px; font-size: 16.5px; line-height: 1.45; font-weight: 600; }}
.card h2 a {{ color: var(--ink); text-decoration: none; }}
.card h2 a:hover {{ color: var(--accent-dark); }}
.meta {{
  display: flex; flex-wrap: wrap; gap: 5px 14px; color: var(--ink-faint);
  font-size: 12.5px; margin-bottom: 7px;
}}
.badge {{
  display: inline-block; padding: 1px 8px; border-radius: 4px; font-size: 11.5px;
  background: #eef4f1; color: var(--accent-dark); font-weight: 500;
}}
.badge.cat {{ background: #f0efe9; color: #6b6455; }}
.excerpt {{ margin: 0 0 9px; color: var(--ink-soft); font-size: 13.5px; }}
.open {{ font-size: 13px; text-decoration: none; }}
.open:hover {{ text-decoration: underline; }}

.empty {{ text-align: center; color: var(--ink-faint); padding: 50px 20px; }}
footer.site {{
  border-top: 1px solid var(--line); padding: 22px 20px 40px;
  color: var(--ink-faint); font-size: 12.5px;
}}
footer.site p {{ margin: 0 0 7px; }}
@media (max-width: 560px) {{
  header.site h1 {{ font-size: 20px; }}
  .card h2 {{ font-size: 15.5px; }}
  main, nav.filters {{ padding-left: 14px; padding-right: 14px; }}
}}
</style>
</head>
<body>
<header class="site">
  <div class="wrap">
    <h1>{title}</h1>
    <div class="sub">微信公众号 · 小红书 · 抖音　|　飞盘相关讯息聚合</div>
    <div class="stats" id="stats"></div>
  </div>
</header>
<div class="rule"></div>

<nav class="filters">
  <div class="wrap">
    <div class="row" id="catRow"></div>
    <div class="row">
      <div id="platRow" style="display:flex;gap:8px;flex-wrap:wrap"></div>
      <input class="search" id="q" type="search" placeholder="搜标题、作者…" autocomplete="off">
      <button class="chip" id="sortBtn" aria-pressed="false">按热度排</button>
    </div>
  </div>
</nav>

<main><div class="wrap" id="list"></div></main>

<footer class="site">
  <div class="wrap">
    <p><b>关于本站</b>　只收录训练/技巧与队伍赛事两类内容，自动过滤媒体报道、
       行业分析和装备种草。每条都给出原文链接，内容版权归原作者所有。</p>
    <p>数据由本地程序采集后发布为静态页面，<span id="gen"></span>。
       本站不存储全文、不提供下载，仅作信息索引。</p>
    <p>如有内容不希望被收录，请联系站长删除。</p>
  </div>
</footer>

<script>
const DATA_URL = "data.json";
let DATA = null, cat = "all", plat = "all", q = "", byHot = false;

const el = (id) => document.getElementById(id);

function matches(it) {{
  if (cat !== "all" && it.category !== cat) return false;
  if (plat !== "all" && it.platformLabel !== plat) return false;
  if (q) {{
    const hay = (it.title + " " + it.author).toLowerCase();
    if (!hay.includes(q)) return false;
  }}
  return true;
}}

function render() {{
  const list = el("list");
  let items = DATA.items.filter(matches);
  if (byHot) items = items.slice().sort((a, b) => b.metric - a.metric);

  if (!items.length) {{
    list.innerHTML = '<div class="empty">没有匹配的内容</div>';
    return;
  }}

  list.innerHTML = items.map((it) => `
    <article class="card">
      <h2><a href="${{it.url}}" target="_blank" rel="noopener noreferrer">${{escapeHtml(it.title)}}</a></h2>
      <div class="meta">
        <span class="badge">${{escapeHtml(it.platformLabel)}}</span>
        <span class="badge cat">${{escapeHtml(it.categoryLabel)}}</span>
        ${{it.author ? `<span>@${{escapeHtml(it.author)}}</span>` : ""}}
        ${{it.date ? `<span>${{it.date}}</span>` : "<span>时间未知</span>"}}
        ${{it.metric ? `<span>${{it.metricLabel}} ${{fmt(it.metric)}}</span>` : ""}}
      </div>
      ${{it.excerpt ? `<p class="excerpt">${{escapeHtml(it.excerpt)}}</p>` : ""}}
      <a class="open" href="${{it.url}}" target="_blank" rel="noopener noreferrer">打开原文 →</a>
    </article>`).join("");
}}

function escapeHtml(s) {{
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}}
function fmt(n) {{
  if (n >= 100000000) return (n / 100000000).toFixed(1) + "亿";
  if (n >= 10000) return (n / 10000).toFixed(1) + "万";
  return String(n);
}}

function buildFilters() {{
  const cats = [{{ key: "all", label: "全部", count: DATA.total }}].concat(DATA.categories);
  el("catRow").innerHTML = cats.map((c) =>
    `<button class="chip" data-cat="${{c.key}}" aria-pressed="${{c.key === cat}}">
       ${{escapeHtml(c.label)}}<span class="n">${{c.count}}</span></button>`).join("");

  const plats = [{{ key: "all", label: "全部平台", count: DATA.total }}]
    .concat(Object.entries(DATA.platforms).map(([k, v]) => ({{ key: k, label: k, count: v }})));
  el("platRow").innerHTML = plats.map((p) =>
    `<button class="chip" data-plat="${{p.key}}" aria-pressed="${{p.key === plat}}">
       ${{escapeHtml(p.label)}}<span class="n">${{p.count}}</span></button>`).join("");

  const total = Object.values(DATA.platforms).reduce((a, b) => a + b, 0);
  el("stats").innerHTML =
    `最近更新 <b>${{DATA.generatedAt}}</b>　收录 <b>${{DATA.total}}</b> 条` +
    `　覆盖 <b>${{Object.keys(DATA.platforms).length}}</b> 个平台` +
    (DATA.latestDate ? `　最新内容 <b>${{DATA.latestDate}}</b>` : "");
  el("gen").textContent = "最近更新：" + DATA.generatedAt;
}}

el("catRow").addEventListener("click", (e) => {{
  const b = e.target.closest("[data-cat]"); if (!b) return;
  cat = b.dataset.cat; buildFilters(); render();
}});
el("platRow").addEventListener("click", (e) => {{
  const b = e.target.closest("[data-plat]"); if (!b) return;
  plat = b.dataset.plat; buildFilters(); render();
}});
el("q").addEventListener("input", (e) => {{ q = e.target.value.trim().toLowerCase(); render(); }});
el("sortBtn").addEventListener("click", (e) => {{
  byHot = !byHot;
  e.target.setAttribute("aria-pressed", String(byHot));
  e.target.textContent = byHot ? "按时间排" : "按热度排";
  render();
}});

fetch(DATA_URL)
  .then((r) => {{ if (!r.ok) throw new Error(r.status); return r.json(); }})
  .then((d) => {{ DATA = d; buildFilters(); render(); }})
  .catch((err) => {{
    el("list").innerHTML =
      '<div class="empty">数据加载失败：' + escapeHtml(err.message) +
      '<br>如果你是直接双击打开的本地文件，浏览器会拦截 data.json 读取。<br>' +
      '请用本地服务器打开，或者直接访问部署好的网址。</div>';
  }});
</script>
</body>
</html>
"""


def build_index_html(title: str, payload: dict) -> str:
    return PAGE_TEMPLATE.format(
        title=html.escape(title),
        description=html.escape(
            f"{title} —— 微信公众号、小红书、抖音上飞盘相关的训练技巧与队伍赛事动态，"
            f"共 {payload['total']} 条，最近更新 {payload['generatedAt']}。"
        ),
        brand=BRAND,
        accent=ACCENT,
        accent_dark=ACCENT_DARK,
    )


def content_signature(payload: dict) -> str:
    """内容指纹：忽略 generatedAt，只看真实内容。

    用途是判断「这次生成跟上次有没有区别」。区别只在时间戳上时，
    不该重写文件、不该产生提交、更不该把网站的「最近更新」刷新掉 ——
    那会让人以为有新内容，而实际上什么都没有。

    用户明确要求过：没有新内容就保留原来的，不要假装更新过。
    """
    meaningful = {k: v for k, v in payload.items() if k != "generatedAt"}
    blob = json.dumps(meaningful, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def generate_site(
    posts: list[Post],
    out_dir: Path,
    classifier: CategoryClassifier,
    *,
    title: str = "飞盘讯息聚合",
    generated_at: datetime | None = None,
    keep_history: bool = True,
    skip_if_unchanged: bool = False,
) -> dict:
    """生成静态站到 out_dir。

    skip_if_unchanged=True 时，先比内容指纹：和现有 data.json 一致就直接
    返回，什么都不写。定时任务用这个模式，避免"没内容也刷新一遍"。

    keep_history=True 时会把本次数据也存一份到 archive/ 下，
    这样即使后面某次采集结果变少，历史页面也还在。
    """
    payload = build_payload(posts, classifier, generated_at=generated_at)

    existing = out_dir / "data.json"
    if skip_if_unchanged and existing.exists():
        try:
            old = json.loads(existing.read_text(encoding="utf-8"))
            if content_signature(old) == content_signature(payload):
                return {
                    "out_dir": out_dir,
                    "unchanged": True,
                    "total": old.get("total", 0),
                    "generatedDate": old.get("generatedDate", ""),
                    "latestDate": old.get("latestDate", ""),
                    "categories": {
                        c["label"]: c["count"] for c in old.get("categories", [])
                    },
                    "platforms": old.get("platforms", {}),
                }
        except Exception:
            # 旧的 data.json 坏了就照常重写，不要因为这个卡住发布
            pass

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (out_dir / "index.html").write_text(
        build_index_html(title, payload), encoding="utf-8"
    )
    # 让 GitHub Pages 不要用 Jekyll 处理（否则 _ 开头的文件会被忽略）
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    if keep_history:
        archive = out_dir / "archive"
        archive.mkdir(exist_ok=True)
        (archive / f"{payload['generatedDate']}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        _prune_archive(archive, keep_days=180)

    return {
        "out_dir": out_dir,
        "unchanged": False,
        "total": payload["total"],
        "generatedDate": payload["generatedDate"],
        "latestDate": payload["latestDate"],
        "categories": {c["label"]: c["count"] for c in payload["categories"]},
        "platforms": payload["platforms"],
    }


def _prune_archive(archive: Path, keep_days: int) -> None:
    """归档目录只留最近 N 天，免得仓库无限膨胀。"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    for path in archive.glob("*.json"):
        if path.stem < cutoff:
            path.unlink(missing_ok=True)


def copy_site_into(src: Path, dest: Path) -> None:
    """把生成好的站点复制到另一个目录（发布时用）。"""
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "data.json", ".nojekyll"):
        source = src / name
        if source.exists():
            shutil.copy2(source, dest / name)
    archive_src = src / "archive"
    if archive_src.exists():
        archive_dest = dest / "archive"
        archive_dest.mkdir(exist_ok=True)
        for path in archive_src.glob("*.json"):
            shutil.copy2(path, archive_dest / path.name)
