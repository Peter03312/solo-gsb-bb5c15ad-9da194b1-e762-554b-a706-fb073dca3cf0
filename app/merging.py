"""重复试剂档案合并（先预览后确认 · 单次确认令牌 · 单事务迁移）。

合并方向：把**来源档案（source）**的全部步骤引用迁移到**保留档案（target）**，
随后删除来源档案。为避免"预览后数据已变仍按旧预览提交"，流程分两步：

1. ``preview_merge``：不落库，返回受影响步骤数量、两侧名称 / 类别与逐方案明细，
   并签发一个**仅对本次预览有效**的确认令牌（内存存储、一次性、带 TTL），
   令牌绑定预览时刻的档案属性指纹与受影响步骤快照指纹；
2. ``confirm_merge``：重新计算两侧属性与受影响步骤快照，与令牌比对——
   任一档案属性或任一受影响步骤发生变化（含步骤被删除 / 迁出、新增引用，
   以及预览后经安全确认接口再次确认导致确认时间刷新，即便措施 / 备注 /
   确认指纹未变），拒绝提交（409）并要求重新预览；通过后在**单个事务**内完成：
   步骤引用迁移 → 步骤展示快照（试剂名 / 类别）更新 → 已确认步骤安全确认
   转待复核 → 删除来源档案。任一步失败整体回滚，不留下部分迁移。

历史推演报告（runs.report）是自包含 JSON 快照，合并过程不触碰；
合并后方案详情 / 列表立即按保留档案解析名称与类别。
"""
import hashlib
import json
import secrets
import threading
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from . import models, safety

# 确认令牌有效期（秒）：仅服务于"预览 → 确认"这一次短时交互
TOKEN_TTL_SECONDS = 10 * 60


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


# 纳入档案属性指纹的全部字段（任一字段变化都必须重新预览）
_REAGENT_ATTR_FIELDS = (
    "name",
    "cas",
    "formula",
    "concentration",
    "state",
    "categories",
    "note",
    "builtin",
)


def _reagent_fingerprint(reagent: models.Reagent) -> str:
    attrs = {key: getattr(reagent, key) for key in _REAGENT_ATTR_FIELDS}
    attrs["categories"] = json.loads(reagent.categories or "[]")
    return _sha(attrs)


def _affected_steps(db: Session, source_id: int) -> list[models.Step]:
    """受影响步骤 = 当前引用来源档案 ID 的全部加液步骤（临时试剂快照不受影响）。"""
    return (
        db.query(models.Step)
        .filter(models.Step.reagent_id == source_id)
        .order_by(models.Step.plan_id, models.Step.order_index, models.Step.id)
        .all()
    )


# 受影响步骤纳入快照指纹的字段：迁移只改 reagent_id / reagent_name / categories
# 与安全待复核标记，故步骤本身的引用关系、顺序、用量、时刻、备注、既有安全
# 确认数据只要变化（含步骤被删除）就与预览不一致，必须拒绝并重新预览。
# 安全确认数据同时包含确认时间 safety_confirmed_at：即便措施 / 备注 / 确认指纹
# 均未变，预览后经安全确认接口再次确认（确认时间被刷新）也视为步骤已变化，
# 不能按旧预览执行迁移。
_STEP_SNAPSHOT_FIELDS = (
    "id",
    "plan_id",
    "order_index",
    "reagent_id",
    "reagent_name",
    "categories",
    "amount_ml",
    "time_offset_min",
    "note",
    "safety_confirmed",
    "safety_stale",
)


def _step_fingerprint(step: models.Step) -> str:
    parts = {key: getattr(step, key) for key in _STEP_SNAPSHOT_FIELDS}
    parts["categories"] = json.loads(step.categories or "[]")
    parts["safety_measures"] = json.loads(step.safety_measures or "[]")
    parts["safety_note"] = step.safety_note
    parts["safety_confirmed_fp"] = step.safety_confirmed_fp
    parts["safety_confirmed_at"] = (
        step.safety_confirmed_at.isoformat() if step.safety_confirmed_at else None
    )
    return _sha(parts)


def _reagent_side(reagent: models.Reagent) -> dict:
    return {
        "id": reagent.id,
        "name": reagent.name,
        "cas": reagent.cas,
        "formula": reagent.formula,
        "concentration": reagent.concentration,
        "state": reagent.state,
        "categories": json.loads(reagent.categories or "[]"),
        "note": reagent.note,
        "builtin": bool(reagent.builtin),
        "created_at": reagent.created_at.isoformat() if reagent.created_at else None,
    }


def _plans_index(db: Session, plan_ids) -> dict[int, models.Plan]:
    if not plan_ids:
        return {}
    rows = db.query(models.Plan).filter(models.Plan.id.in_(plan_ids)).all()
    return {p.id: p for p in rows}


def _preview_payload(
    db: Session, source: models.Reagent, target: models.Reagent
) -> tuple[dict, str, str, str, list[int]]:
    """构建预览响应，同时返回确认校验所需的各指纹。"""
    steps = _affected_steps(db, source.id)
    plans = _plans_index(db, {s.plan_id for s in steps})

    affected_plans: dict[int, dict] = {}
    for s in steps:
        plan = plans.get(s.plan_id)
        bucket = affected_plans.setdefault(
            s.plan_id,
            {
                "plan_id": s.plan_id,
                "plan_name": plan.name if plan else f"（已删除方案 #{s.plan_id}）",
                "step_count": 0,
                "confirmed_step_count": 0,
                "steps": [],
            },
        )
        bucket["step_count"] += 1
        if s.safety_confirmed:
            bucket["confirmed_step_count"] += 1
        bucket["steps"].append(
            {
                "step_id": s.id,
                "order_index": s.order_index,
                "reagent_name": s.reagent_name,
                "amount_ml": s.amount_ml,
                "time_offset_min": s.time_offset_min,
                "note": s.note,
                "safety_confirmed": bool(s.safety_confirmed),
                "safety_stale": bool(s.safety_stale),
            }
        )

    plans_detail = [affected_plans[pid] for pid in sorted(affected_plans)]
    confirmed_steps = sum(p["confirmed_step_count"] for p in plans_detail)
    # 合并后会显式转为待复核的步骤数：当前确认有效（指纹匹配且未被标记待复核）的步骤。
    # 原本就未确认 / 已待复核的步骤不额外计数。
    review_reset = sum(
        1 for s in steps if safety.step_safety_state(db, s)["confirmed"]
    )
    payload = {
        "kind": "labmix-reagent-merge-preview",
        "source": _reagent_side(source),
        "target": _reagent_side(target),
        "affected_step_count": len(steps),
        "affected_plan_count": len(plans_detail),
        "confirmed_step_count": confirmed_steps,
        "will_reset_to_review_count": review_reset,
        "affected_plans": plans_detail,
        "previewed_at": _utcnow_iso(),
    }
    step_ids = [s.id for s in steps]
    source_fp = _reagent_fingerprint(source)
    target_fp = _reagent_fingerprint(target)
    steps_fp = _sha([_step_fingerprint(s) for s in steps])
    return payload, source_fp, target_fp, steps_fp, step_ids


# ---------------- 一次性确认令牌（仅对本次预览有效） ----------------


class _TokenStore:
    """进程内令牌存储：一次性消费、带 TTL。

    应用为单容器部署（docker-compose），令牌只需在"预览 → 确认"的短时交互内
    存活；服务重启后旧令牌自动失效（拒绝并要求重新预览），符合预期。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def issue(self, record: dict, ttl: int = TOKEN_TTL_SECONDS) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._purge_locked()
            record = dict(record)
            record["expires_at"] = time.monotonic() + ttl
            self._items[token] = record
        return token

    def consume(self, token: str) -> dict | None:
        """取出并立即销毁令牌（一次性）；不存在 / 已过期返回 None。"""
        with self._lock:
            self._purge_locked()
            return self._items.pop(token, None)

    def _purge_locked(self) -> None:
        now = time.monotonic()
        expired = [t for t, rec in self._items.items() if rec["expires_at"] <= now]
        for t in expired:
            self._items.pop(t, None)


_tokens = _TokenStore()


# ---------------- 预览 / 确认 ----------------


def _get_pair(db: Session, source_id: int, target_id: int):
    if source_id == target_id:
        raise HTTPException(422, "来源档案与保留档案不能是同一条试剂档案")
    source = db.get(models.Reagent, source_id)
    if source is None:
        raise HTTPException(404, f"来源试剂 id={source_id} 不存在")
    target = db.get(models.Reagent, target_id)
    if target is None:
        raise HTTPException(404, f"保留试剂 id={target_id} 不存在")
    return source, target


def preview_merge(db: Session, source_id: int, target_id: int) -> dict:
    """合并预览：不落库，返回影响面并签发仅本次预览有效的确认令牌。"""
    source, target = _get_pair(db, source_id, target_id)
    payload, source_fp, target_fp, steps_fp, step_ids = _preview_payload(
        db, source, target
    )
    token = _tokens.issue(
        {
            "source_id": source.id,
            "target_id": target.id,
            "source_fp": source_fp,
            "target_fp": target_fp,
            "steps_fp": steps_fp,
            "step_ids": step_ids,
        }
    )
    payload["confirm_token"] = token
    payload["token_expires_in_seconds"] = TOKEN_TTL_SECONDS
    return payload


def confirm_merge(db: Session, source_id: int, target_id: int, token: str) -> dict:
    """凭预览令牌执行合并；属性 / 步骤已变则拒绝，成功则单事务完成迁移。"""
    if not token or not str(token).strip():
        raise HTTPException(422, "缺少确认令牌：请先调用预览接口获取 confirm_token")
    record = _tokens.consume(str(token).strip())
    if record is None:
        raise HTTPException(
            409,
            "确认令牌无效或已过期（令牌仅对一次预览有效且一次性使用），请重新预览后再确认。",
        )
    if record["source_id"] != source_id or record["target_id"] != target_id:
        raise HTTPException(
            409,
            "确认令牌与本次选择的来源 / 保留档案不一致，请重新选择并预览后再确认。",
        )

    source = db.get(models.Reagent, source_id)
    target = db.get(models.Reagent, target_id)
    missing = []
    if source is None:
        missing.append("来源档案")
    if target is None:
        missing.append("保留档案")
    if missing:
        raise HTTPException(409, f"{'、'.join(missing)}已不存在，请重新预览后再确认。")

    # 乐观校验：预览后任一档案属性变化即拒绝
    if _reagent_fingerprint(source) != record["source_fp"]:
        raise HTTPException(
            409,
            f"来源档案「{source.name}」的属性在预览后已变化，请重新预览后再确认。",
        )
    if _reagent_fingerprint(target) != record["target_fp"]:
        raise HTTPException(
            409,
            f"保留档案「{target.name}」的属性在预览后已变化，请重新预览后再确认。",
        )

    steps = _affected_steps(db, source_id)
    current_ids = [s.id for s in steps]
    if current_ids != record["step_ids"] or _sha([_step_fingerprint(s) for s in steps]) != record["steps_fp"]:
        raise HTTPException(
            409,
            "受影响的方案步骤在预览后已变化（步骤被修改 / 删除、来源档案新增了引用，"
            "或步骤经安全确认接口再次确认导致确认时间变化；措施 / 备注 / 确认指纹"
            "未变但确认时间刷新同样视为已变化），请重新预览后再确认。",
        )

    try:
        # 1) 引用迁移：来源档案 → 保留档案（Core 批量 UPDATE，避免 ORM 关系联动）
        db.execute(
            update(models.Step)
            .where(models.Step.reagent_id == source_id)
            .values(reagent_id=target_id)
        )
        # 2) 更新步骤展示快照：试剂名改为保留档案名；类别对档案试剂统一留空数组，
        #    展示 / 推演时由保留档案实时解析（与新建档案步骤的存储约定一致）。
        db.execute(
            update(models.Step)
            .where(models.Step.reagent_id == target_id, models.Step.id.in_(current_ids))
            .values(reagent_name=target.name, categories="[]")
        )
        # 3) 已完成安全确认的步骤：内容所指试剂已变为保留档案，确认转为待复核；
        #    原本未确认的步骤保持未确认（不额外点亮待复核标记）。
        db.execute(
            update(models.Step)
            .where(
                models.Step.id.in_(current_ids),
                models.Step.safety_confirmed.is_(True),
            )
            .values(safety_stale=True)
        )
        # 4) 删除来源档案（Core DELETE，绕过 ORM 关系处理；此时已无步骤引用，
        #    RESTRICT 外键不会触发）
        db.execute(delete(models.Reagent).where(models.Reagent.id == source_id))
        db.commit()
    except Exception:
        db.rollback()
        raise

    # 历史推演报告（runs.report）保持原快照，不做任何改写。
    db.expire_all()
    migrated_steps = (
        db.query(models.Step).filter(models.Step.id.in_(current_ids)).all()
        if current_ids
        else []
    )
    affected_plan_ids = sorted({s.plan_id for s in migrated_steps})
    plans = _plans_index(db, affected_plan_ids)
    plans_detail = [
        {"plan_id": pid, "plan_name": plans[pid].name}
        for pid in affected_plan_ids
        if pid in plans
    ]
    return {
        "kind": "labmix-reagent-merge-result",
        "source_reagent_id": source_id,
        "target_reagent_id": target_id,
        "target": _reagent_side(target),
        "migrated_step_count": len(migrated_steps),
        "affected_plan_count": len(plans_detail),
        "affected_plans": plans_detail,
        "merged_at": _utcnow_iso(),
    }
