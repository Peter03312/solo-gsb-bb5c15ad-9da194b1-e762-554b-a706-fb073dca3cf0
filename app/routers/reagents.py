"""试剂属性档案管理。"""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import merging, models
from ..database import get_db
from ..schemas import (
    ReagentCreate,
    ReagentMergeConfirmIn,
    ReagentMergeIn,
    ReagentOut,
    ReagentUpdate,
)

router = APIRouter(prefix="/api/reagents", tags=["reagents"])


@router.get("", response_model=list[ReagentOut])
def list_reagents(q: str | None = None, db: Session = Depends(get_db)):
    query = db.query(models.Reagent)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (models.Reagent.name.like(like))
            | (models.Reagent.cas.like(like))
            | (models.Reagent.formula.like(like))
        )
    return query.order_by(models.Reagent.name).all()


@router.post("", response_model=ReagentOut, status_code=201)
def create_reagent(reagent_in: ReagentCreate, db: Session = Depends(get_db)):
    exists = db.query(models.Reagent).filter_by(name=reagent_in.name).first()
    if exists:
        raise HTTPException(409, f"试剂「{reagent_in.name}」已存在")
    reagent = models.Reagent(
        name=reagent_in.name,
        cas=reagent_in.cas,
        formula=reagent_in.formula,
        concentration=reagent_in.concentration,
        state=reagent_in.state,
        categories=json.dumps(reagent_in.categories, ensure_ascii=False),
        note=reagent_in.note,
        builtin=False,
    )
    db.add(reagent)
    db.commit()
    db.refresh(reagent)
    return reagent


@router.post("/merge/preview")
def preview_reagent_merge(payload: ReagentMergeIn, db: Session = Depends(get_db)):
    """重复档案合并预览（不落库）。

    返回受影响的方案步骤数量、逐方案明细、来源 / 保留两侧名称与类别，
    以及一个**仅对本次预览有效**的一次性确认令牌 ``confirm_token``。
    """
    return merging.preview_merge(db, payload.source_reagent_id, payload.target_reagent_id)


@router.post("/merge/confirm")
def confirm_reagent_merge(payload: ReagentMergeConfirmIn, db: Session = Depends(get_db)):
    """凭预览令牌执行合并。

    预览后任一档案属性或受影响步骤变化 → 409 并要求重新预览；受影响步骤
    在预览后经安全确认接口再次确认（措施 / 备注 / 确认指纹未变、仅确认
    时间刷新）同样视为已变化并 409。通过后单事务完成：步骤引用迁移 →
    展示快照更新 → 已确认步骤转待复核 → 删除来源档案。失败时整体回滚，
    不留下部分迁移。
    """
    return merging.confirm_merge(
        db, payload.source_reagent_id, payload.target_reagent_id, payload.confirm_token
    )


@router.get("/{reagent_id}", response_model=ReagentOut)
def get_reagent(reagent_id: int, db: Session = Depends(get_db)):
    reagent = db.get(models.Reagent, reagent_id)
    if reagent is None:
        raise HTTPException(404, "试剂不存在")
    return reagent


@router.put("/{reagent_id}", response_model=ReagentOut)
def update_reagent(
    reagent_id: int, reagent_in: ReagentUpdate, db: Session = Depends(get_db)
):
    reagent = db.get(models.Reagent, reagent_id)
    if reagent is None:
        raise HTTPException(404, "试剂不存在")
    data = reagent_in.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        dup = (
            db.query(models.Reagent)
            .filter(models.Reagent.name == data["name"], models.Reagent.id != reagent_id)
            .first()
        )
        if dup:
            raise HTTPException(409, f"试剂「{data['name']}」已存在")
    if "categories" in data and data["categories"] is not None:
        reagent.categories = json.dumps(data.pop("categories"), ensure_ascii=False)
    for key, value in data.items():
        setattr(reagent, key, value)
    db.commit()
    db.refresh(reagent)
    return reagent


@router.delete("/{reagent_id}", status_code=204)
def delete_reagent(reagent_id: int, db: Session = Depends(get_db)):
    reagent = db.get(models.Reagent, reagent_id)
    if reagent is None:
        raise HTTPException(404, "试剂不存在")
    used = db.query(models.Step).filter_by(reagent_id=reagent_id).count()
    if used:
        raise HTTPException(
            409,
            f"试剂「{reagent.name}」正被 {used} 个加液步骤引用，请先修改相关方案后再删除。",
        )
    db.delete(reagent)
    db.commit()
