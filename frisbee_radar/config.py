"""配置加载。

约定：所有配置都在项目根目录的 config/ 下，YAML 格式。
命令行参数优先级高于配置文件。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .categories import Category
from .relevance import KeywordConfig

# 项目根目录：本文件在 <root>/frisbee_radar/config.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
BROWSER_STATE_DIR = DATA_DIR / "browser_state"


@dataclass
class CrawlSettings:
    limit_per_target: int = 30
    delay_min: float = 3.0
    delay_max: float = 7.0
    max_failures: int = 3
    since_days: int = 30


@dataclass
class PlatformSettings:
    """单个平台的配置。keywords / accounts 是两种互补的采集入口。"""

    name: str
    enabled: bool = True
    mode: str = ""
    keywords: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def option(self, key: str, default: Any = None) -> Any:
        return self.extra.get(key, default)


@dataclass
class WatchSettings:
    """增量产出策略：什么时候才值得生成一份新简报。"""

    authoritative: list[str] = field(default_factory=list)
    trigger_min_new: int = 8
    min_interval_hours: float = 6.0
    authoritative_breaks_interval: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WatchSettings":
        data = data or {}
        return cls(
            authoritative=[
                str(a).strip() for a in (data.get("authoritative") or []) if str(a).strip()
            ],
            trigger_min_new=int(data.get("trigger_min_new", 8)),
            min_interval_hours=float(data.get("min_interval_hours", 6.0)),
            authoritative_breaks_interval=bool(
                data.get("authoritative_breaks_interval", True)
            ),
        )


@dataclass
class OutputSettings:
    title: str = "飞盘日报"
    dir: str = "D:/Desktop/每日飞盘记录"
    filename: str = "{date}_飞盘日报.docx"
    also_json: bool = True
    max_items: int = 80

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "OutputSettings":
        data = data or {}
        return cls(
            title=str(data.get("title", "飞盘日报")),
            dir=str(data.get("dir", "D:/Desktop/每日飞盘记录")),
            filename=str(data.get("filename", "{date}_飞盘日报.docx")),
            also_json=bool(data.get("also_json", True)),
            max_items=int(data.get("max_items", 80)),
        )

    def resolve_dir(self) -> Path:
        """产出目录。

        配的是绝对路径（默认指到桌面）就直接用；写成相对路径则按项目根解析，
        免得受当前工作目录影响。
        """
        path = Path(self.dir)
        return path if path.is_absolute() else PROJECT_ROOT / path

    def resolve_filename(self, moment: datetime | None = None) -> str:
        """文件名，{date} 换成 YYYY-MM-DD（本地日期）。"""
        when = moment or datetime.now()
        return self.filename.format(date=when.strftime("%Y-%m-%d"))


@dataclass
class CategoryMatchSettings:
    """分类匹配策略。默认只看标题 —— 精度优先。"""

    body_min_hits: int = 0
    global_exclude: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CategoryMatchSettings":
        data = data or {}
        return cls(
            body_min_hits=int(data.get("body_min_hits", 0)),
            global_exclude=[
                str(w).strip() for w in (data.get("global_exclude") or []) if str(w).strip()
            ],
        )


@dataclass
class PublishSettings:
    """静态网页发布设置。"""

    title: str = "飞盘讯息聚合"
    dir: str = "docs"
    branch: str = "main"
    # Cloudflare Pages 项目名。填了就同时往 Cloudflare 部署一份，
    # 网址形如 https://<项目名>.pages.dev —— 好处是不含 GitHub 用户名。
    # 留空则只推 GitHub。
    cloudflare_project: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PublishSettings":
        data = data or {}
        return cls(
            title=str(data.get("title", "飞盘讯息聚合")),
            dir=str(data.get("dir", "docs")),
            branch=str(data.get("branch", "main")),
            cloudflare_project=str(data.get("cloudflare_project", "")),
        )

    def resolve_dir(self) -> Path:
        path = Path(self.dir)
        return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass
class AppConfig:
    crawl: CrawlSettings
    keywords: KeywordConfig
    watch: WatchSettings
    output: OutputSettings
    publish: PublishSettings
    categories: list[Category]
    category_match: CategoryMatchSettings
    platforms: dict[str, PlatformSettings]
    data_dir: Path
    config_dir: Path
    browser_state_dir: Path

    def platform(self, name: str) -> PlatformSettings:
        return self.platforms.get(name, PlatformSettings(name=name, enabled=False))

    def build_classifier(self) -> "CategoryClassifier":
        """按配置造一个分类器。

        集中在这里造，免得命令行和 DOCX 生成器各造一个、策略还不一致。
        """
        from .categories import CategoryClassifier

        return CategoryClassifier(
            self.categories,
            body_min_hits=self.category_match.body_min_hits,
            global_exclude=self.category_match.global_exclude,
        )

    def db_path(self) -> Path:
        override = os.environ.get("FRISBEE_DB")
        if override:
            return Path(override)
        return self.data_dir / "frisbee.db"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _build_platform(name: str, raw: dict[str, Any] | None) -> PlatformSettings:
    raw = raw or {}
    reserved = {"enabled", "mode", "keywords", "accounts"}
    return PlatformSettings(
        name=name,
        enabled=bool(raw.get("enabled", True)),
        mode=str(raw.get("mode", "")),
        keywords=[str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip()],
        accounts=[str(a).strip() for a in (raw.get("accounts") or []) if str(a).strip()],
        extra={k: v for k, v in raw.items() if k not in reserved},
    )


def load_config(
    config_dir: Path | None = None,
    data_dir: Path | None = None,
) -> AppConfig:
    config_dir = config_dir or CONFIG_DIR
    data_dir = data_dir or DATA_DIR

    sources_raw = _read_yaml(config_dir / "sources.yaml")
    keywords_raw = _read_yaml(config_dir / "keywords.yaml")

    crawl_raw = sources_raw.get("crawl") or {}
    crawl = CrawlSettings(
        limit_per_target=int(crawl_raw.get("limit_per_target", 30)),
        delay_min=float(crawl_raw.get("delay_min", 3.0)),
        delay_max=float(crawl_raw.get("delay_max", 7.0)),
        max_failures=int(crawl_raw.get("max_failures", 3)),
        since_days=int(crawl_raw.get("since_days", 30)),
    )

    platforms = {
        name: _build_platform(name, sources_raw.get(name))
        for name in ("wechat", "wechat_channels", "xiaohongshu", "douyin")
    }

    browser_state_dir = data_dir / "browser_state"

    return AppConfig(
        crawl=crawl,
        keywords=KeywordConfig.from_dict(keywords_raw),
        watch=WatchSettings.from_dict(sources_raw.get("watch")),
        output=OutputSettings.from_dict(sources_raw.get("output")),
        publish=PublishSettings.from_dict(sources_raw.get("publish")),
        categories=[
            Category.from_dict(c) for c in (sources_raw.get("categories") or [])
            if isinstance(c, dict)
        ],
        category_match=CategoryMatchSettings.from_dict(
            sources_raw.get("category_match")
        ),
        platforms=platforms,
        data_dir=data_dir,
        config_dir=config_dir,
        browser_state_dir=browser_state_dir,
    )
