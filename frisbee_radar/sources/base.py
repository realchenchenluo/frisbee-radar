"""采集器基类与公共工具。"""

from __future__ import annotations

import asyncio
import random
import sys
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import AppConfig, PlatformSettings


class SourceError(RuntimeError):
    """采集过程中的可预期失败（未登录、被限流、页面改版等）。"""


def sleep_random(low: float, high: float) -> None:
    """随机间隔，避免固定节奏被识别为脚本。"""
    return asyncio.sleep(random.uniform(max(0.0, low), max(0.0, high)))


def parse_timestamp(value: Any) -> datetime | None:
    """把平台给的各种时间格式统一成 datetime。

    见过的形态：秒级 int、毫秒级 int、ISO 字符串、"2024-05-01 10:00"。
    """
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        ts = float(value)
        # 毫秒时间戳（13 位）换算成秒
        if ts > 1e11:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        return parse_timestamp(int(text))

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
        "%Y年%m月%d日",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            continue

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_int(value: Any) -> int:
    """把 "1.2万" / "3,456" / 1234 之类统一成 int。"""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().replace(",", "").replace("+", "")
    if not text:
        return 0

    multiplier = 1
    for suffix, mult in (("万", 10_000), ("w", 10_000), ("W", 10_000), ("亿", 100_000_000)):
        if text.endswith(suffix):
            multiplier = mult
            text = text[: -len(suffix)]
            break
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


class BaseSource(ABC):
    """所有平台采集器的父类。

    子类只需要实现 `crawl_target`：给定一个关键词或账号，产出 Post 列表。
    关键词/账号的遍历、限速、异常隔离由基类统一处理——这样单个关键词
    被风控不会让整个平台的采集挂掉。
    """

    platform: str = "base"

    def __init__(self, settings: PlatformSettings, config: AppConfig):
        self.settings = settings
        self.config = config
        self.crawl_cfg = config.crawl

    @abstractmethod
    async def crawl_target(self, target: str, kind: str, limit: int) -> list[Any]:
        """抓取单个关键词(kind='keyword')或账号(kind='account')。"""

    async def crawl(self, only: str | None = None) -> AsyncIterator[tuple[str, str, list[Any]]]:
        """遍历所有目标，逐条 yield (target, kind, posts)。

        每个目标之间插入随机延迟；连续失败 max_failures 次后放弃该平台
        （通常意味着登录态失效或已被限流，继续跑只会加重风控）。
        """
        settings = self.settings
        targets: list[tuple[str, str]] = []
        targets += [(k, "keyword") for k in settings.keywords]
        targets += [(a, "account") for a in settings.accounts]

        if only:
            targets = [t for t in targets if t[0] == only]
            if not targets:
                raise SourceError(f"{self.platform} 的配置里没有目标 {only!r}")

        failures = 0
        for index, (target, kind) in enumerate(targets):
            if index > 0:
                await sleep_random(self.crawl_cfg.delay_min, self.crawl_cfg.delay_max)
            try:
                posts = await self.crawl_target(
                    target, kind, self.crawl_cfg.limit_per_target
                )
                failures = 0
                yield target, kind, posts
            except Exception as exc:  # noqa: BLE001 - 单目标失败不该中断整轮
                failures += 1
                # 立刻把原因打出来。原来是静默 yield []，详细提示（比如
                # 「可能是未登录，双击 7-用我的浏览器.bat」）要等连续失败
                # max_failures 次才冒出来 —— 单目标场景下永远看不到，
                # 用户只看到「没有拿到内容」，无从排查。
                first_line = str(exc).splitlines()[0] if str(exc) else ""
                print(
                    f"[!] {self.platform} 的「{target}」采集失败："
                    f"{type(exc).__name__}: {first_line}",
                    file=sys.stderr, flush=True,
                )
                for line in str(exc).splitlines()[1:]:
                    print(f"    {line}", file=sys.stderr, flush=True)
                yield target, kind, []
                if failures >= self.crawl_cfg.max_failures:
                    raise SourceError(
                        f"{self.platform} 连续 {failures} 个目标失败，已停止。"
                        f"最后一次错误：{exc}"
                    ) from exc

    async def close(self) -> None:
        """释放资源，子类按需覆盖。"""
