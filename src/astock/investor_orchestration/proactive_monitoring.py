"""Capability-neutral, complete-universe plans for proactive investment monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from astock.investor_orchestration.models import InvestorSessionPreflightReceipt
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService


@dataclass(frozen=True)
class NativeMonitorPlan:
    subjects: tuple[str, ...]
    windows: tuple[str, ...]
    prompt: str
    local_fallback: str
    subject_batches: tuple[tuple[str, ...], ...] = ()


class ProactiveMonitorPlanner:
    """Keep the complete universe; an execution budget bounds batches, not membership.

    Native creation and successful execution remain separately verified facts.
    The plan neither grants permissions nor places real broker orders.
    """

    def __init__(self, subjects: ResearchSubjectRegistryService, config_path: Path) -> None:
        self.subjects = subjects
        value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("monitor configuration must be a mapping")
        self.config = value

    def plan(
        self, preflight: InvestorSessionPreflightReceipt, *, retained_subjects: tuple[str, ...] = ()
    ) -> NativeMonitorPlan | None:
        held = {
            item.instrument_id
            for lane in (preflight.context.actual, preflight.context.paper)
            for item in lane.positions
        }
        watch = {
            item.instrument_id for item in self.subjects.current_watchlist(as_of=preflight.as_of)
        }
        subjects = tuple(sorted(held | watch | set(retained_subjects)))
        if not subjects:
            return None
        batch_size = int(self.config.get("max_subjects_per_run", 50))
        if batch_size <= 0:
            raise ValueError("monitor batch size must be positive")
        batches = tuple(
            subjects[index : index + batch_size] for index in range(0, len(subjects), batch_size)
        )
        windows: list[str] = []
        labels = {
            "PRE_OPEN": "盘前",
            "INTRADAY": "盘中",
            "POST_CLOSE": "收盘后",
            "pre_open": "盘前",
            "intraday": "盘中",
            "post_close": "收盘后",
        }
        for name, raw in self.config["windows"].items():
            label = labels.get(name, "定期复核")
            stable_name = str(name).upper()
            if "local_time" in raw:
                value = str(raw["local_time"])
                windows.append(f"{label} {value}（{stable_name}@{value}）")
            else:
                windows.extend(
                    f"{label} {value}（{stable_name}@{value}）"
                    for value in raw.get("local_times", ())
                )
        prompt = (
            "从正式账户和研究记录刷新实盘、模拟盘及待入场观察清单，分批核实新增的重要变化。"
            "优先核实价格、公告、行业与政策变化，仅复核受影响的研究环节。"
            "只有资料覆盖充分且确无实质变化时才保持安静；获取失败不能视为没有持仓或没有风险。"
            "未完成的批次应保留并继续核对，不得因执行预算遗漏持仓。"
            "只报告会改变投资判断、风险或进入时机的事项；不得下真实交易指令。"
        )
        return NativeMonitorPlan(
            subjects=subjects,
            windows=tuple(windows),
            prompt=prompt,
            local_fallback="investor schedule-tick（现有本地交易日时钟）",
            subject_batches=batches,
        )
