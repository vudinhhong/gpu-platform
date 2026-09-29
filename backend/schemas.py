"""Pydantic v2 request / response schemas.

Naming convention:
  *Create   – payload for POST endpoints (creation)
  *Update   – payload for PUT endpoints  (partial update, all fields optional)
  *Response – shape returned to the caller
  *WithXxx  – response that embeds a related object
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator


# ---------------------------------------------------------------------------
# Shared config helper
# ---------------------------------------------------------------------------

def _orm_config() -> ConfigDict:
    """ConfigDict that enables reading from SQLAlchemy ORM instances."""
    return ConfigDict(from_attributes=True)


# ===========================================================================
# Auth
# ===========================================================================

class Token(BaseModel):
    access_token: str
    token_type: str


class LoginResponse(Token):
    """Full login payload: token + user info."""
    user: "UserResponse"


# ===========================================================================
# Users
# ===========================================================================

class UserBase(BaseModel):
    username:  str
    email:     str
    full_name: Optional[str] = None
    is_admin:  bool = False


class UserCreate(UserBase):
    password: str
    # Quotas may be set at creation time; None = platform default.
    disk_quota_mb:   Optional[int]   = None
    # Per budget period (QUOTA_PERIOD): GPU-hours and CPU core-hours.
    gpu_hours_quota: Optional[float] = None
    cpu_hours_quota: Optional[float] = None
    # Existing directory on the host to use as this user's workspace.
    home_path:       Optional[str]   = None


class UserUpdate(BaseModel):
    """All fields optional – only supplied values are applied."""
    email:     Optional[str]  = None
    full_name: Optional[str]  = None
    is_admin:  Optional[bool] = None
    is_active: Optional[bool] = None
    # 0 clears the quota back to the platform default.
    disk_quota_mb:   Optional[int]   = None
    gpu_hours_quota: Optional[float] = None
    cpu_hours_quota: Optional[float] = None
    # "" clears the mapping and returns the user to a platform-owned workspace.
    home_path:       Optional[str]   = None


class PasswordReset(BaseModel):
    new_password: str


class ProfileUpdate(BaseModel):
    """What a user may change about themselves.

    Deliberately narrower than :class:`UserUpdate`: a user edits their own
    name and email, never their quotas, their admin flag or their workspace
    path.  Every field is optional, the form sends only what it touched.
    """

    email:     Optional[str] = None
    full_name: Optional[str] = None


class SelfPasswordChange(BaseModel):
    """A user changing their own password.

    ``reset_jupyter_password`` only means anything for the few users who set a
    *separate* Jupyter password: with it the new account password takes over
    there too, without it their second factor is left alone.
    """

    old_password: str
    new_password: str
    reset_jupyter_password: bool = True


class UserResponse(UserBase):
    id:         int
    is_active:  bool
    created_at: datetime
    # Whether the user has configured a personal Jupyter password (never the
    # password/hash itself, owner UI uses this to show the config panel).
    jupyter_password_set: bool = False
    disk_quota_mb:   Optional[int]   = None
    gpu_hours_quota: Optional[float] = None
    cpu_hours_quota: Optional[float] = None
    preferred_image: Optional[str]   = None
    home_path:       Optional[str]   = None

    model_config = _orm_config()


class UserWithAssignment(UserResponse):
    """User profile including their GPU assignment (if any)."""
    gpu_assignment: Optional["GpuAssignmentResponse"] = None

    model_config = _orm_config()


# ===========================================================================
# GPU Assignments
# ===========================================================================

class GpuAssignmentCreate(BaseModel):
    """What an administrator may set for one user.

    Every limit is optional, including the GPUs: capping someone's CPU and RAM
    without giving them a GPU at all is an ordinary thing to want, and the
    form used to make it impossible by requiring a GPU to be picked.  What is
    NOT allowed is an assignment that sets nothing, which would be a row with
    no meaning.
    """

    user_id:           int
    gpu_indices:       List[int]       = []     # e.g. [0, 1]; empty = no GPU
    memory_limit_mb:   Optional[int]   = None
    cpu_cores:         Optional[float] = None   # cgroup cpu.max
    # RLIMIT_CPU, process backend only; the admin form no longer offers it.
    cpu_limit_seconds: Optional[int]   = None
    max_processes:     Optional[int]   = None   # cgroup pids.max


class GpuAssignmentUpdate(BaseModel):
    """A partial update.

    Which fields were *sent* is what matters, not whether they are null: the
    admin form sends every field on every save, so a field blanked there
    arrives as null and must clear the limit.  A field the caller leaves out
    entirely is left alone.  ``model_fields_set`` tells the two apart.
    """

    gpu_indices:       Optional[List[int]] = None
    memory_limit_mb:   Optional[int]       = None
    cpu_cores:         Optional[float]     = None
    cpu_limit_seconds: Optional[int]       = None
    max_processes:     Optional[int]       = None


class GpuAssignmentResponse(BaseModel):
    id:                int
    user_id:           int
    gpu_indices:       List[int]     # deserialized from "0,1" storage format
    memory_limit_mb:   Optional[int]
    cpu_cores:         Optional[float] = None
    cpu_limit_seconds: Optional[int]
    max_processes:     Optional[int] = None
    created_at:        datetime
    updated_at:        datetime

    model_config = _orm_config()

    @field_validator("gpu_indices", mode="before")
    @classmethod
    def _parse_gpu_indices(cls, v: object) -> List[int]:
        """Convert comma-separated string stored in DB back to a list of ints."""
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v  # type: ignore[return-value]


class GpuAssignmentWithUser(GpuAssignmentResponse):
    """Assignment record plus the owning user's basic info."""
    user: UserResponse

    model_config = _orm_config()


# ===========================================================================
# Jupyter Sessions
# ===========================================================================

class JupyterSessionResponse(BaseModel):
    id:            int
    user_id:       int
    port:          int
    pid:           Optional[int]
    container_id:  Optional[str] = None
    image:         Optional[str] = None
    ssh_port:      Optional[int] = None
    ssh_password:  Optional[str] = None
    status:        str   # SessionStatus enum value coerced to str
    token:         str
    base_url:      str
    created_at:    datetime
    last_activity: datetime

    model_config = _orm_config()


class JupyterSessionWithUser(JupyterSessionResponse):
    """Session record plus the owning user's basic info."""
    user: UserResponse

    model_config = _orm_config()


# ===========================================================================
# GPU Hardware Status  (returned by nvidia-smi, not stored in DB)
# ===========================================================================

class GpuProcess(BaseModel):
    pid:            int
    name:           str
    memory_used_mb: int


class GpuInfo(BaseModel):
    index:               int
    name:                str
    total_memory_mb:     int
    used_memory_mb:      int
    free_memory_mb:      int
    gpu_utilization:     int   # percent 0-100
    memory_utilization:  int   # percent 0-100
    temperature:         int   # degrees Celsius
    processes:           List[GpuProcess]


# ===========================================================================
# Generic
# ===========================================================================

class MessageResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Rebuild forward references (required by Pydantic v2 for self-referencing)
# ---------------------------------------------------------------------------
LoginResponse.model_rebuild()
UserWithAssignment.model_rebuild()
GpuAssignmentWithUser.model_rebuild()
JupyterSessionWithUser.model_rebuild()
