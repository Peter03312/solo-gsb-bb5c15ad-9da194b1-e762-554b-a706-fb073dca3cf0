"""待加试剂插入位置评估（预览，不落库）。

在**已保存方案**的每一个可插入位置（首位、相邻步骤之间、末尾）逐一预演：
把一股待加试剂插入该位置后，沿用方案当前生效规则版本（没有则全局启用规则图），
按推演引擎既有的有向 / 无向类别匹配与"首次 high 即中止"口径推演。

每个位置返回：
- 首次危险（finding 依据：规则编号 / 方程式 / 机理 / 文献 / 类别对 / 步骤）；
- 因中止而**被阻断的原步骤**（0 基原方案编号）；
- 命中依据（全部 finding）与逐步时间轴。

推荐位次（严格按序）：
1. 无 high 命中（推演不中止）的位置优先；
2. 均有 high 时，被阻断的原步骤最少（即未被阻断的原步骤最多）者优先；
3. 再按警示（medium）命中数少者优先；
4. 仍并列时位置靠前者优先。

纯只读计算：不保存步骤、不写入推演记录、不要求安全确认。
"""
from __future__ import annotations

from . import engine

# 位次排序键（越小越优）：
#   有 high(1) 优先于无 high(0)？—— 无 high 更优，故有 high=1
#   被阻断原步骤数越少越好；警示命中数越少越好；位置越靠前越好
def _rank_key(pos: dict):
    return (
        1 if pos["has_danger"] else 0,
        len(pos["blocked_original_steps"]),
        pos["finding_counts"]["medium"],
        pos["position"],
    )


def position_label(n_existing: int, position: int) -> str:
    """人类可读的位次描述（步骤序号按 1 基展示）。"""
    if position == 0:
        return "首位（第 1 步之前）"
    if position == n_existing:
        return f"末尾（第 {n_existing} 步之后）"
    return f"第 {position} 步与第 {position + 1} 步之间"


def evaluate_placement(base_steps: list[engine.InputStep], candidate: engine.InputReagent,
                       amount_ml: float, note: str | None,
                       rules, revision: str) -> dict:
    """对全部 N+1 个插入位置逐一预演并给出推荐。

    base_steps: 已保存方案按序排列的引擎输入步骤（order_index 为 0..N-1）；
    candidate:  待加试剂（名称 + 类别）；
    rules/revision: 方案当前生效规则版本快照，或全局启用规则图。
    """
    n = len(base_steps)
    candidate_step = engine.InputStep(
        order_index=0,
        reagent=candidate,
        amount_ml=amount_ml,
        note=note,
    )

    positions: list[dict] = []
    for pos in range(n + 1):
        # 组装插入后的步骤序列并重排 0 基顺序；
        # orig_index[i] 表示新序列第 i 步对应的原方案步骤（插入步为 None）
        merged = list(base_steps[:pos]) + [candidate_step] + list(base_steps[pos:])
        orig_index: list[int | None] = []
        for new_idx in range(len(merged)):
            new_order = new_idx
            merged[new_idx] = engine.InputStep(
                order_index=new_order,
                reagent=merged[new_idx].reagent,
                time_offset_min=merged[new_idx].time_offset_min,
                amount_ml=merged[new_idx].amount_ml,
                note=merged[new_idx].note,
            )
            if new_order < pos:
                orig_index.append(new_order)
            elif new_order == pos:
                orig_index.append(None)
            else:
                orig_index.append(new_order - 1)

        report = engine.simulate(merged, rules, revision)
        findings = report["findings"]

        # 新序列 → 原方案步骤编号的映射
        def to_original(new_step: int | None) -> int | None:
            if new_step is None:
                return None
            return orig_index[new_step]

        first_danger = None
        if report["first_danger"] is not None:
            fd = report["first_danger"]
            first_danger = {
                **fd,
                # 引擎给出的是插入后序列的步骤编号，换算回原方案编号便于对照
                "inserted_step": fd["step"],
                "conflicts_with_inserted_step": fd["conflicts_with_step"],
                "step_original": to_original(fd["step"]),
                "conflicts_with_step_original": to_original(fd["conflicts_with_step"]),
                "step_is_inserted": fd["step"] == pos,
                "conflicts_with_is_inserted": fd["conflicts_with_step"] == pos,
            }

        # 被阻断步骤：仅统计原方案步骤（待加试剂自身单列）
        blocked_original_steps = sorted(
            {to_original(s) for s in report["blocked_steps"] if to_original(s) is not None}
        )
        inserted_blocked = (pos in report["blocked_steps"])
        warning_original_steps = sorted(
            {
                to_original(t["order_index"])
                for t in report["timeline"]
                if t["state"] == "warning" and to_original(t["order_index"]) is not None
            }
        )

        counts = {"high": report["summary"]["high"],
                  "medium": report["summary"]["medium"],
                  "low": report["summary"]["low"]}

        # 精简时间轴：标注每步是否为待加试剂及其原方案步骤号
        timeline = []
        for t in report["timeline"]:
            orig = to_original(t["order_index"])
            timeline.append({
                "order_index": t["order_index"],
                "original_order_index": orig,
                "is_inserted": t["order_index"] == pos,
                "reagent_name": t["reagent_name"],
                "state": t["state"],
                "state_label": t["state_label"],
                "finding_ids": t["finding_ids"],
            })

        # 待加试剂在该位置的加入结果（ok / warning / danger / blocked）
        inserted_timeline = next((t for t in timeline if t["is_inserted"]), None)
        inserted_state = inserted_timeline["state"] if inserted_timeline else "blocked"
        inserted_state_label = {
            "ok": "相容，已加入体系",
            "warning": "发生警示反应，已加入体系",
            "danger": "发生严重反应，未安全加入",
            "blocked": "已阻断（前序步骤已中止，未加入）",
        }[inserted_state]

        # finding 命中依据：换算步骤编号并标注是否涉及待加试剂
        findings_out = []
        for f in findings:
            findings_out.append({
                **f,
                "incoming_step_original": to_original(f["incoming_step"]),
                "existing_step_original": to_original(f["existing_step"]),
                "incoming_is_inserted": f["incoming_step"] == pos,
                "existing_is_inserted": f["existing_step"] == pos,
            })

        positions.append({
            "position": pos,
            "label": position_label(n, pos),
            "after_step": pos - 1,          # 插在该原步骤之后（-1 = 首位）
            "before_step": pos if pos < n else None,  # 插在该原步骤之前（null = 末尾）
            "status": report["status"],
            "status_label": report["status_label"],
            "has_danger": report["halted"],
            "inserted_state": inserted_state,
            "inserted_state_label": inserted_state_label,
            "first_danger": first_danger,
            "blocked_original_steps": blocked_original_steps,
            "blocked_original_count": len(blocked_original_steps),
            "inserted_blocked": inserted_blocked,
            "warning_original_steps": warning_original_steps,
            "executed_original_count": n - len(blocked_original_steps),
            "finding_counts": counts,
            "findings": findings_out,
            "timeline": timeline,
        })

    best_idx = min(range(len(positions)), key=lambda i: _rank_key(positions[i]))
    best = positions[best_idx]
    recommended = {
        "position": best["position"],
        "label": best["label"],
        "after_step": best["after_step"],
        "before_step": best["before_step"],
        "has_danger": best["has_danger"],
        "blocked_original_count": best["blocked_original_count"],
        "warning_hit_count": best["finding_counts"]["medium"],
        "reason": _recommend_reason(best, positions, n),
        "rank_basis": {
            "rule": "no_danger > max_unblocked_original > fewer_warnings > earlier_position",
            "has_danger": best["has_danger"],
            "blocked_original_count": best["blocked_original_count"],
            "executed_original_count": best["executed_original_count"],
            "warning_hit_count": best["finding_counts"]["medium"],
            "position": best["position"],
        },
    }

    return {
        "kind": "labmix-placement-preview",
        "rule_revision": revision,
        "original_step_count": n,
        "position_count": n + 1,
        "positions": positions,
        "recommended": recommended,
    }


def _recommend_reason(best: dict, all_positions: list[dict], n: int) -> str:
    """生成人类可读的推荐理由。"""
    no_danger = [p for p in all_positions if not p["has_danger"]]
    if not best["has_danger"]:
        if len(no_danger) == len(all_positions):
            reason = (f"所有 {len(all_positions)} 个位置推演均不中止；"
                      f"该位置警示命中 {best['finding_counts']['medium']} 条"
                      "（并列时警示最少、位置最靠前）。")
        else:
            reason = (f"共 {len(no_danger)} 个位置无严重危险（推演不中止），优先推荐；"
                      f"该位置警示命中 {best['finding_counts']['medium']} 条"
                      "（并列时警示最少、位置最靠前）。")
        if best["finding_counts"]["medium"] == 0 and best["finding_counts"]["low"] == 0:
            reason += " 该位置全程无任何警示 / 提示命中。"
        return reason

    # 所有位置均有 high：选未被阻断的原步骤最多者
    return (
        f"所有 {len(all_positions)} 个位置均会触发严重危险并中止推演；"
        f"该位置仅阻断 {best['blocked_original_count']} 个原步骤"
        f"（{n - best['blocked_original_count']} 个原步骤可执行），"
        "被阻断原步骤最少者优先；"
        f"警示命中 {best['finding_counts']['medium']} 条，并列时警示最少、位置最靠前。"
    )
