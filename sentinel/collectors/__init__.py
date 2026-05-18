"""Collector 注册表 + auto_discover。"""
from sentinel.collectors.base import BaseCollector, CollectedMessage


KIND_REGISTRY: dict[str, type[BaseCollector]] = {}


def register(cls: type[BaseCollector]) -> type[BaseCollector]:
    """注册 collector 类到 KIND_REGISTRY。"""
    if not cls.KIND:
        raise ValueError(f"{cls.__name__} 缺少 KIND 类属性")
    KIND_REGISTRY[cls.KIND] = cls
    return cls


def auto_discover() -> None:
    """import 各 collector 模块，触发 @register。

    新增 collector 模块时在此 import 列表加一行；模块未实现则跳过。
    """
    import importlib

    for module_name in (
        "sentinel.collectors.telegram",
        "sentinel.collectors.rss",
        "sentinel.collectors.twitter",
        "sentinel.collectors.reddit",
        "sentinel.collectors.social",
        "sentinel.collectors.file_inbox",
    ):
        try:
            importlib.import_module(module_name)
        except ImportError:
            pass


__all__ = [
    "BaseCollector",
    "CollectedMessage",
    "KIND_REGISTRY",
    "register",
    "auto_discover",
]
