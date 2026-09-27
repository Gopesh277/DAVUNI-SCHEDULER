from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class CourseIn(BaseModel):
    faculty: str
    nature: str = "Regular"
    course_code: str
    course_name: str
    course_type: str = ""
    programme: str
    semester: str = ""
    theory_hrs: int = 0
    tutorial_hrs: int = 0
    practical_hrs: int = 0
    emp_id: Optional[str] = None


class PeriodIn(BaseModel):
    index: int
    label: str


class ConfigIn(BaseModel):
    rooms: list[str]
    lab_rooms: list[str]
    days: list[str]
    periods: list[PeriodIn]
    day_break_after_period: int = 4
    practical_block_size: int = 2
    max_consecutive_periods: int = Field(2, ge=1, le=10)
    position_daily_caps: dict[str, int] = Field(default_factory=dict)
    default_position_daily_cap: int = Field(5, ge=1, le=20)


class SolverSettingsIn(BaseModel):
    time_limit: int = Field(60, ge=1, le=600)
    workers: int = Field(8, ge=1, le=32)
    balance: bool = True


class GenerateRequest(BaseModel):
    # Same bounds as SolverSettingsIn -- these override the saved settings
    # for a single call, so they need the same guardrails against an
    # absurd (or malicious) worker/time-limit value reaching OR-Tools.
    time_limit: Optional[int] = Field(None, ge=1, le=600)
    workers: Optional[int] = Field(None, ge=1, le=32)
    balance: Optional[bool] = None


class LoginIn(BaseModel):
    username: str
    password: str


class PlaceRequest(BaseModel):
    """Drag an unplaced session onto a specific slot."""
    session_id: int
    day: int = Field(..., ge=0)
    start_period: int = Field(..., ge=1)
    room: str


class MoveRequest(BaseModel):
    """Drag an already-placed session to a different slot."""
    session_id: int
    day: int = Field(..., ge=0)
    start_period: int = Field(..., ge=1)
    room: str


class UnplaceRequest(BaseModel):
    """Drag a placed session back out to the unplaced tray."""
    session_id: int
