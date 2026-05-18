"""LLM usage accumulator · ServiceShell 内累计每次 run 的 LLM 调用成本。

设计：contextvars + mutable list，调用方透明。

  ServiceShell.__aenter__ → begin_collect() 设当前 run 的 acc 列表
  _call_cli / _call_api → record_call(usage) 把每次调用 usage 追加到 acc
  ServiceShell.__aexit__ → totals(acc) 聚合写 service_runs

monkeypatch 测试时不触发 record_call，service_runs 里 llm_* 列默认 0。
asyncio.to_thread 通过 copy_context 浅拷贝，list 引用 share，sub-thread
append 主线程可见。
"""
from __future__ import annotations

import contextvars
from typing import Optional


_USAGE_VAR: contextvars.ContextVar[Optional[list[dict]]] = contextvars.ContextVar(
    "llm_usage", default=None)


def begin_collect() -> list[dict]:
    """开启本次 run 的 usage 累加；返回 acc 引用（caller 持有）。"""
    acc: list[dict] = []
    _USAGE_VAR.set(acc)
    return acc


def end_collect() -> None:
    """关闭本次 run 的 usage 累加（防止后续误 record）。"""
    _USAGE_VAR.set(None)


def record_call(usage: dict) -> None:
    """LLM call 完成时调；usage = {input_tokens, output_tokens, cost_usd, ...}。"""
    acc = _USAGE_VAR.get()
    if acc is not None:
        acc.append(usage)


def totals(acc: list[dict]) -> dict:
    """聚合 list[usage] → {llm_tokens_in, llm_tokens_out, llm_cost_usd}。"""
    return {
        "llm_tokens_in": sum(u.get("input_tokens", 0) for u in acc),
        "llm_tokens_out": sum(u.get("output_tokens", 0) for u in acc),
        "llm_cache_read_tokens": sum(
            u.get("cache_read_input_tokens", 0) for u in acc),
        "llm_cache_creation_tokens": sum(
            u.get("cache_creation_input_tokens", 0) for u in acc),
        "llm_cost_usd": round(sum(u.get("cost_usd", 0.0) for u in acc), 6),
    }
