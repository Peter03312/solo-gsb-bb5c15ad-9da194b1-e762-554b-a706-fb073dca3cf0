"""待加试剂插入位置评估（纯预览，不落库）。

在**已保存方案**中评估"一股待加试剂"放在首位 / 相邻步骤之间 / 末尾哪个位置更合适：
对 n 个原步骤的方案逐一预演 n+1 个插入位置，每个位置都把候选试剂作为一个新步骤
插入该位置后整体重新推演，规则来源沿用该方案**当前生效规则版本**（没有时使用
全局启用规则图）。

每个位置沿用既有引擎口径（有向 / 无向类别匹配、首次 high 命中即中止），返回：
首次危险（发生在候选步骤还是原步骤、与谁冲突、命中规则依据）、被阻断的**原步骤**、
以及候选试剂参与的全部命中依据。

推荐位置决胜顺序：
  1. 优先无 high（候选与原步骤均未触发严重）的位置；
  2. 若全部位置都有 high：被阻断的原步骤最少（即未被阻断的原步骤最多）；
  3. 警示（medium/low）命中数少；
  4. 位置靠前。

评估只做内存预演：不保存方案步骤、不产生推演记录，也不触发安全确认门槛。
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import deduce, engine, models

# 候选试剂在推演序列中的标记
KIND_CANDIDATE = "candidate"
KIND_ORIGINAL = "original"


@dataclass
class Candidate:
    name: str
    categories: list[str]
    amount_ml: float
    reagent_id: int | None = None


def _finding_basis(finding: dict) -> dict:
    """抽取 finding 的可追溯判定依据（与导出报告同口径）。"""
    return {
        "rule_id": finding["rule_id"],
        "severity": finding["severity"],
        "severity_label": finding["severity_label"],
        "title": finding["title"],
        "reaction": finding["reaction"],
        "rationale": finding["rationale"],
        "references": finding["references"],
        "incoming_category": finding["incoming_category"],
        "existing_category": finding["existing_category"],
        "match_direction": finding["match_direction"],
        "rule_directional": finding["rule_directional"],
    }


def _position_label(position: int, n: int) -> str:
    if position == 0:
        return "首位（第 1 步之前）" if n else "首位（空方案）"
    if position == n:
        return "末尾（最后一步之后）"
    return f"第 {position} 步与第 {position + 1} 步之间"


def _build_inserted_steps(
    base_steps: list[engine.InputStep], candidate: Candidate, position: int
) -> list[engine.InputStep]:
    """把候选试剂插到 position 处（0..n），并重排为连续 0 基推演序号。

    原步骤以浅拷贝进入推演序列并重排序号，不污染调用方的 base_steps。
    """
    inserted = engine.InputStep(
        order_index=position,
        reagent=engine.InputReagent(name=candidate.name, categories=list(candidate.categories)),
        amount_ml=candidate.amount_ml,
        note=None,
    )
    seq = (
        [_copy_step(s) for s in base_steps[:position]]
        + [inserted]
        + [_copy_step(s) for s in base_steps[position:]]
    )
    for idx, step in enumerate(seq):
        step.order_index = idx
    return seq


def _copy_step(step: engine.InputStep) -> engine.InputStep:
    return engine.InputStep(
        order_index=step.order_index,
        time_offset_min=step.time_offset_min,
        amount_ml=step.amount_ml,
        note=step.note,
        reagent=engine.InputReagent(
            name=step.reagent.name,
            categories=list(step.reagent.categories),
        ),
    )


def _sim_index_to_original(sim_index: int, position: int) -> int | None:
    """推演序号 -> 原方案 0 基步骤序号；候选位置返回 None。"""
    if sim_index == position:
        return None
    return sim_index if sim_index < position else sim_index - 1


def _evaluate_position(
    base_steps: list[engine.InputStep],
    candidate: Candidate,
    position: int,
    rules,
    revision: str,
    baseline_warning_count: int,
) -> dict:
    n = len(base_steps)
    seq = _build_inserted_steps(base_steps, candidate, position)
    report = engine.simulate(seq, rules, revision)

    # 推演序号 -> 原步骤序号（候选为 None）
    sim_to_orig = {i: _sim_index_to_original(i, position) for i in range(n + 1)}
    timeline = report["timeline"]
    by_sim = {t["order_index"]: t for t in timeline}
    candidate_entry = by_sim[position]

    # ---- 首次危险：映射回候选 / 原步骤编号 ----
    first_danger = None
    if report["first_danger"] is not None:
        fd = report["first_danger"]
        danger_orig = sim_to_orig[fd["step"]]
        other_orig = sim_to_orig[fd["conflicts_with_step"]]
        first_danger = {
            "step_kind": KIND_CANDIDATE if danger_orig is None else KIND_ORIGINAL,
            "candidate_step": position,
            "step": danger_orig,  # 原步骤序号；危险发生在候选步骤时为 None
            "step_label": "待加试剂" if danger_orig is None else f"原第 {danger_orig + 1} 步",
            "reagent_name": fd["reagent_name"],
            "conflicts_with_kind": (
                KIND_CANDIDATE if other_orig is None else KIND_ORIGINAL
            ),
            "conflicts_with_step": other_orig,
            "conflicts_with_reagent": fd["conflicts_with_reagent"],
            "finding_id": fd["finding_id"],
            **_finding_basis(
                next(f for f in report["findings"] if f["id"] == fd["finding_id"])
            ),
        }

    # ---- 被阻断的原步骤（候选自身阻断单列）----
    blocked_sims = set(report["blocked_steps"])
    candidate_blocked = position in blocked_sims
    blocked_original = sorted(
        sim_to_orig[s] for s in blocked_sims if sim_to_orig[s] is not None
    )

    # ---- 候选试剂参与的全部命中依据 ----
    candidate_findings = []
    for f in report["findings"]:
        if f["incoming_step"] != position and f["existing_step"] != position:
            continue
        counterpart_sim = (
            f["existing_step"] if f["incoming_step"] == position else f["incoming_step"]
        )
        counterpart_orig = sim_to_orig[counterpart_sim]
        candidate_findings.append(
            {
                **_finding_basis(f),
                "finding_id": f["id"],
                "incoming_step_kind": (
                    KIND_CANDIDATE if f["incoming_step"] == position else KIND_ORIGINAL
                ),
                "existing_step_kind": (
                    KIND_CANDIDATE if f["existing_step"] == position else KIND_ORIGINAL
                ),
                "counterpart_original_step": counterpart_orig,
                "counterpart_reagent": (
                    f["existing_reagent"]
                    if f["incoming_step"] == position
                    else f["incoming_reagent"]
                ),
            }
        )

    warning_count = report["summary"]["medium"] + report["summary"]["low"]
    high_count = report["summary"]["high"]

    # 候选是否引入 high：候选参与的命中中是否存在 high 级
    # （基线方案本身的原有 high 不算候选引入——用户关心的是"这股试剂"放哪不新增严重风险）
    candidate_high = any(f["severity"] == "high" for f in candidate_findings)
    candidate_first_danger = (
        first_danger is not None and first_danger["step_kind"] == KIND_CANDIDATE
    )
    # 候选在该位置能否安全加入：既不卷入 high、也不被体系此前已发生的首次危险阻断
    candidate_safe = not candidate_high and not candidate_blocked

    # 紧凑时间轴（标注候选 / 原步骤映射），供网页逐步展示
    compact_timeline = []
    for t in timeline:
        orig = sim_to_orig[t["order_index"]]
        compact_timeline.append(
            {
                "sim_index": t["order_index"],
                "kind": KIND_CANDIDATE if orig is None else KIND_ORIGINAL,
                "original_step": orig,
                "reagent_name": t["reagent_name"],
                "state": t["state"],
                "state_label": t["state_label"],
            }
        )

    return {
        "position": position,
        "position_label": _position_label(position, n),
        "candidate_step": position,
        "status": report["status"],
        "has_high": high_count > 0,
        # 候选试剂本身是否触发 / 卷入 high 命中——推荐"无 high 位置"的判定字段；
        # 原方案基线自带的 high 不影响该字段
        "candidate_triggers_high": candidate_high,
        "candidate_safe": candidate_safe,
        "candidate_first_danger": candidate_first_danger,
        "candidate_state": candidate_entry["state"],
        "candidate_state_label": candidate_entry["state_label"],
        "candidate_blocked": candidate_blocked,
        "first_danger": first_danger,
        "blocked_original_steps": blocked_original,
        "blocked_original_count": len(blocked_original),
        "executed_original_step_count": n - len(blocked_original),
        "warning_findings_count": warning_count,
        "warning_findings_delta": warning_count - baseline_warning_count,
        "high_findings_count": high_count,
        "candidate_findings": candidate_findings,
        "timeline": compact_timeline,
    }


def _recommend(positions: list[dict], n: int, baseline_halted: bool) -> dict:
    """按决胜顺序挑选推荐位置并给出理由。

    优先候选能安全加入（不触发 high 且不被阻断）的位置（原方案自身既有的首次危险
    仍在报告中展示）；若每个位置候选都会卷入 high 或被阻断，则按被阻断原步骤最少、
    警示命中少、位置靠前决胜。
    """
    safe = [p for p in positions if p["candidate_safe"]]
    pool = safe if safe else positions

    def sort_key(p: dict) -> tuple:
        if safe:
            return (p["warning_findings_count"], p["position"])
        # 无候选可安全加入的位置：被阻断原步骤最少 → 警示命中少 → 位置靠前
        return (p["blocked_original_count"], p["warning_findings_count"], p["position"])

    best = min(pool, key=sort_key)

    if safe:
        earliest = best["position"] == min(p["position"] for p in safe)
        delta_word = "增加" if best["warning_findings_delta"] >= 0 else "减少"
        base_note = (
            "待加试剂在该位置可安全加入、不触发新的严重（high）规则"
            "（原方案自身的首次危险仍在下方展示）"
            if baseline_halted
            else "该位置待加试剂可安全加入、不会触发严重（high）规则"
        )
        reason = (
            f"{base_note}；在候选可安全加入的位置中，其参与的警示命中 "
            f"{best['warning_findings_count']} 条（较原方案{delta_word} "
            f"{abs(best['warning_findings_delta'])} 条）"
            + ("，并为位置最靠前者。" if earliest else "，按警示最少原则入选。")
        )
        tier = "no_high"
    else:
        reason = (
            f"所有 {n + 1} 个位置待加试剂都会触发严重（high）规则或被体系已有危险阻断；"
            f"该位置被阻断的原步骤最少（{best['blocked_original_count']} 个原步骤被阻断，"
            f"{best['executed_original_step_count']}/{n} 个原步骤可执行），"
            f"且警示命中 {best['warning_findings_count']} 条、位置最靠前。"
        )
        tier = "least_blocked"

    return {
        "position": best["position"],
        "position_label": best["position_label"],
        "tier": tier,
        "reason": reason,
        "scores": {
            "candidate_safe": best["candidate_safe"],
            "candidate_triggers_high": best["candidate_triggers_high"],
            "has_high": best["has_high"],
            "blocked_original_count": best["blocked_original_count"],
            "executed_original_step_count": best["executed_original_step_count"],
            "warning_findings_count": best["warning_findings_count"],
            "warning_findings_delta": best["warning_findings_delta"],
        },
    }


def evaluate_insertion(db: Session, plan: models.Plan, candidate: Candidate) -> dict:
    """对已保存方案逐一预演候选试剂的全部插入位置，返回各位置结果与推荐。"""
    rules, revision, version_id, scope = deduce.resolve_rule_source(db, plan)
    base_steps = deduce.plan_to_input(db, plan)
    n = len(base_steps)

    # 原方案基线（用于警示命中增减对照）
    baseline = engine.simulate(base_steps, rules, revision)
    baseline_warning = baseline["summary"]["medium"] + baseline["summary"]["low"]
    baseline_blocked = list(baseline["blocked_steps"])

    positions = [
        _evaluate_position(
            base_steps, candidate, p, rules, revision, baseline_warning
        )
        for p in range(n + 1)
    ]
    recommendation = _recommend(positions, n, baseline["halted"])

    return {
        "kind": "labmix-insertion-preview",
        "plan_id": plan.id,
        "plan_name": plan.name,
        "candidate": {
            "reagent_id": candidate.reagent_id,
            "name": candidate.name,
            "categories": candidate.categories,
            "amount_ml": candidate.amount_ml,
        },
        "original_step_count": n,
        "position_count": n + 1,
        "rule_scope": scope,
        "rule_revision": revision,
        **({"rule_version_id": version_id} if version_id is not None else {}),
        "baseline": {
            "status": baseline["status"],
            "status_label": baseline["status_label"],
            "halted": baseline["halted"],
            "first_danger": baseline["first_danger"],
            "blocked_steps": baseline_blocked,
            "warning_findings_count": baseline_warning,
        },
        "recommendation": recommendation,
        "positions": positions,
    }
