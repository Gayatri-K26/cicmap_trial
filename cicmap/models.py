"""The shared output format every parser returns. The database layer only knows these models."""
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class CurriculumEntry(BaseModel):
    """One course as listed in a program's requirements."""
    code: str
    title: Optional[str] = None
    credits_text: Optional[str] = None
    requirement_group: Optional[str] = None
    is_alternative: bool = False
    detail_url: Optional[str] = None


class ProgramData(BaseModel):
    name: Optional[str] = None
    degree: Optional[str] = None
    degree_full: Optional[str] = None
    college: Optional[str] = None
    department: Optional[str] = None
    catalog_year: Optional[str] = None
    total_credits: Optional[str] = None
    description: Optional[str] = None
    entries: List[CurriculumEntry] = Field(default_factory=list)
    header_text: Optional[str] = None  # top-of-page text, used to fill metadata gaps with the LLM
    # Set when this page only summarizes the curriculum and links to the full list elsewhere.
    curriculum_url: Optional[str] = None


class CourseData(BaseModel):
    code: str
    title: Optional[str] = None
    credits_text: Optional[str] = None
    credits_min: Optional[float] = None
    credits_max: Optional[float] = None
    description: Optional[str] = None
    prereq_text: Optional[str] = None
    coreq_text: Optional[str] = None
    restrictions: Optional[str] = None
    extra: Dict[str, str] = Field(default_factory=dict)
    source_url: Optional[str] = None
