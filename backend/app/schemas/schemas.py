from datetime import date, datetime
from pydantic import BaseModel, Field

from app.models.attendance import ApprovalStatus, MissingPunchStatus, PunchType


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    leader_id: int | None = None
    attendance_auto_audit_enabled: bool

    model_config = {"from_attributes": True}


class MissingPunchCreate(BaseModel):
    work_date: date = Field(..., description="yyyy-MM-dd")
    punch_type: PunchType


class MissingPunchOut(BaseModel):
    id: int
    user_id: int
    work_date: date
    punch_type: PunchType
    status: MissingPunchStatus
    created_at: datetime
    repaired_at: datetime | None = None

    model_config = {"from_attributes": True}


class AgentChatRequest(BaseModel):
    message: str
    session_id: int | None = None


class ApprovalOut(BaseModel):
    id: int
    missing_punch_id: int
    applicant_id: int
    leader_id: int
    status: ApprovalStatus
    comment: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None
    applicant_name: str
    work_date: date
    punch_type: PunchType
    reason: str | None = None


class ApprovalReviewRequest(BaseModel):
    approved: bool
    comment: str | None = None


class ChatMessageOut(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatSessionOut(BaseModel):
    id: int
    user_id: int
    missing_punch_id: int | None = None
    repair_work_date: date | None = None
    repair_punch_type: PunchType | None = None
    repair_status: MissingPunchStatus | None = None
    title: str
    status: str
    current_node: str = "start"
    created_at: datetime
    updated_at: datetime
    last_message: str | None = None
    message_count: int = 0


class ChatSessionDetailOut(BaseModel):
    id: int
    user_id: int
    missing_punch_id: int | None = None
    repair_work_date: date | None = None
    repair_punch_type: PunchType | None = None
    repair_status: MissingPunchStatus | None = None
    title: str
    status: str
    current_node: str = "start"
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessageOut]

