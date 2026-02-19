from dataclasses import dataclass
from typing import Optional


@dataclass
class Job:
    job_id: str
    chat_id: int
    status: str          # pending | running | done | failed | needs_user
    result_url: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
