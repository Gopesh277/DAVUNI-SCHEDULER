from __future__ import annotations

from typing import Annotated, Optional
from pydantic import BaseModel, Field

# Upper bounds on everything a client can send. Without them one request (e.g.
# theory_hrs = 1e9, or a million rooms) makes the server build a huge model and
# run out of memory -- taking down every department, since they share a process.
MAX_HOURS_PER_ROW = 40
MAX_COURSE_ROWS = 5000
MAX_TEXT = 300
MAX_ROOMS = 500
MAX_DAYS = 14
MAX_PERIODS = 24

Text = Annotated[str, Field(max_length=MAX_TEXT)]
Hours = Annotated[int, Field(ge=0, le=MAX_HOURS_PER_ROW)]


class CourseIn(BaseModel):
    faculty: Text
    nature: Text = "Regular"
    course_code: Text
    course_name: Text
    course_type: Text = ""
    programme: Text
    semester: Text = ""
    theory_hrs: Hours = 0
    tutorial_hrs: Hours = 0
    practical_hrs: Hours = 0
    emp_id: Optional[str] = None


class PeriodIn(BaseModel):
    index: int = Field(..., ge=1, le=MAX_PERIODS)
    label: Annotated[str, Field(max_length=50)]


class ConfigIn(BaseModel):
    rooms: list[Annotated[str, Field(max_length=100)]] = Field(..., max_length=MAX_ROOMS)
    lab_rooms: list[Annotated[str, Field(max_length=100)]] = Field(..., max_length=MAX_ROOMS)
    days: list[Annotated[str, Field(max_length=50)]] = Field(..., max_length=MAX_DAYS)
    periods: list[PeriodIn] = Field(..., max_length=MAX_PERIODS)
    day_break_after_period: int = Field(4, ge=0, le=MAX_PERIODS)
    practical_block_size: int = Field(2, ge=1, le=6)
    max_consecutive_periods: int = Field(2, ge=1, le=10)
    position_daily_caps: dict[Annotated[str, Field(max_length=100)], Annotated[int, Field(ge=1, le=20)]] = Field(
        default_factory=dict, max_length=50)
    default_position_daily_cap: int = Field(5, ge=1, le=20)


class SolverSettingsIn(BaseModel):
    # An omitted field keeps the department's currently saved value -- there
    # is no hidden default here that could override what the user chose.
    time_limit: Optional[int] = Field(None, ge=1, le=600)
    workers: Optional[int] = Field(None, ge=1, le=32)
    balance: Optional[bool] = None


class GenerateRequest(BaseModel):
    # Same bounds as SolverSettingsIn -- these override the saved settings
    # for a single call, so they need the same guardrails against an
    # absurd (or malicious) worker/time-limit value reaching OR-Tools.
    time_limit: Optional[int] = Field(None, ge=1, le=600)
    workers: Optional[int] = Field(None, ge=1, le=32)
    balance: Optional[bool] = None


class LoginIn(BaseModel):
    department: str = ""
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class PlaceRequest(BaseModel):
    """Drag an unplaced session onto a specific slot."""
    session_id: int
    day: int = Field(..., ge=0)
    start_period: int = Field(..., ge=1)
    room: Annotated[str, Field(max_length=100)]


class MoveRequest(BaseModel):
    """Drag an already-placed session to a different slot."""
    session_id: int
    day: int = Field(..., ge=0)
    start_period: int = Field(..., ge=1)
    room: Annotated[str, Field(max_length=100)]


class UnplaceRequest(BaseModel):
    """Drag a placed session back out to the unplaced tray."""
    session_id: int
