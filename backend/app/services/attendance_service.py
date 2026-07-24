from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import ApprovalStatus, MissingPunch, MissingPunchStatus, RepairApproval, RepairLog
from app.models.user import User
from app.schemas.schemas import ApprovalOut, MissingPunchCreate


async def create_missing_punch(db: AsyncSession, user: User, data: MissingPunchCreate) -> MissingPunch:
    # 员工在“忘记打卡录入”页面新增一条待补卡数据，初始状态由数据库默认值 PENDING 控制。
    item = MissingPunch(user_id=user.id, work_date=data.work_date, punch_type=data.punch_type)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def list_my_missing_punches(db: AsyncSession, user: User, only_open: bool = False) -> list[MissingPunch]:
    # 查询当前员工的补卡记录；only_open=True 时只查可继续处理的记录。
    stmt = select(MissingPunch).where(MissingPunch.user_id == user.id).order_by(MissingPunch.work_date.desc(), MissingPunch.id.desc())
    if only_open:
        stmt = stmt.where(MissingPunch.status.in_([MissingPunchStatus.pending, MissingPunchStatus.approval_pending]))
    return list((await db.execute(stmt)).scalars().all())


async def repair_missing_punch(db: AsyncSession, missing: MissingPunch, operator_id: int, message: str) -> MissingPunch:
    # 执行补卡。当前版本是更新本系统状态为 REPAIRED 并写日志。
    # 如果后续要对接真实考勤系统，建议在这里调用外部补卡 API。
    missing.status = MissingPunchStatus.repaired
    missing.repaired_at = datetime.now(timezone.utc)
    db.add(RepairLog(missing_punch_id=missing.id, operator_id=operator_id, action="REPAIR", message=message))
    await db.commit()
    await db.refresh(missing)
    return missing


async def ensure_pending_approval(db: AsyncSession, missing: MissingPunch, applicant: User, leader: User) -> RepairApproval:
    # 确保有一张待审核单：如果已存在待审核单就复用，避免重复提交。
    result = await db.execute(
        select(RepairApproval).where(
            RepairApproval.missing_punch_id == missing.id,
            RepairApproval.status == ApprovalStatus.pending,
        )
    )
    approval = result.scalar_one_or_none()
    if approval:
        return approval
    missing.status = MissingPunchStatus.approval_pending
    approval = RepairApproval(missing_punch_id=missing.id, applicant_id=applicant.id, leader_id=leader.id)
    db.add(approval)
    db.add(RepairLog(missing_punch_id=missing.id, operator_id=applicant.id, action="REQUEST_APPROVAL", message="提交领导审核"))
    await db.commit()
    await db.refresh(approval)
    return approval


async def list_leader_approvals(db: AsyncSession, leader: User) -> list[ApprovalOut]:
    # 领导审核页面使用：只查询当前领导名下 PENDING 状态的审核单。
    stmt = (
        select(RepairApproval, MissingPunch, User)
        .join(MissingPunch, RepairApproval.missing_punch_id == MissingPunch.id)
        .join(User, RepairApproval.applicant_id == User.id)
        .where(RepairApproval.leader_id == leader.id, RepairApproval.status == ApprovalStatus.pending)
        .order_by(RepairApproval.created_at.asc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        ApprovalOut(
            id=a.id,
            missing_punch_id=a.missing_punch_id,
            applicant_id=a.applicant_id,
            leader_id=a.leader_id,
            status=a.status,
            comment=a.comment,
            created_at=a.created_at,
            reviewed_at=a.reviewed_at,
            applicant_name=u.full_name,
            work_date=m.work_date,
            punch_type=m.punch_type,
            reason=m.reason,
        )
        for a, m, u in rows
    ]


async def review_approval(db: AsyncSession, approval: RepairApproval, reviewer: User, approved: bool, comment: str | None) -> RepairApproval:
    # 领导处理审核单：
    # - 通过：更新审核单为 APPROVED，并立即调用 repair_missing_punch 自动补卡。
    # - 拒绝：更新补卡记录为 REJECTED，并写拒绝日志。
    approval.status = ApprovalStatus.approved if approved else ApprovalStatus.rejected
    approval.comment = comment
    approval.reviewed_at = datetime.now(timezone.utc)
    missing = approval.missing_punch
    if approved:
        await repair_missing_punch(db, missing, reviewer.id, "领导审核通过后自动补卡")
    else:
        missing.status = MissingPunchStatus.rejected
        db.add(RepairLog(missing_punch_id=missing.id, operator_id=reviewer.id, action="REJECT", message=comment or "领导拒绝"))
        await db.commit()
    await db.refresh(approval)
    return approval

