"""采集器注册表。"""

from __future__ import annotations

from ..config import AppConfig
from .base import BaseSource, SourceError
from .douyin import DouyinSource
from .mock import MockSource
from .wechat import WechatSource
from .wechat_channels import WechatChannelsSource
from .xiaohongshu import XiaohongshuSource

SOURCE_CLASSES: dict[str, type[BaseSource]] = {
    "wechat": WechatSource,
    "wechat_channels": WechatChannelsSource,
    "xiaohongshu": XiaohongshuSource,
    "douyin": DouyinSource,
    "mock": MockSource,
}

# 命令行 --source 可用的名字
AVAILABLE = ("wechat", "wechat_channels", "xiaohongshu", "douyin", "mock")


def build_source(name: str, config: AppConfig) -> BaseSource:
    cls = SOURCE_CLASSES.get(name)
    if cls is None:
        raise SourceError(
            f"未知平台 {name!r}。可用：{', '.join(AVAILABLE)}"
        )
    if name == "mock":
        # 独立的 settings 实例：调用方会往 extra 里塞 platform，
        # 不能污染 wechat 的真实配置
        from ..config import PlatformSettings

        return cls(PlatformSettings(name="mock", keywords=["飞盘"]), config)
    return cls(config.platform(name), config)


def enabled_platforms(config: AppConfig) -> list[str]:
    return [
        name for name in ("wechat", "wechat_channels", "xiaohongshu", "douyin")
        if config.platform(name).enabled
    ]


__all__ = [
    "BaseSource",
    "SourceError",
    "SOURCE_CLASSES",
    "AVAILABLE",
    "build_source",
    "enabled_platforms",
]
