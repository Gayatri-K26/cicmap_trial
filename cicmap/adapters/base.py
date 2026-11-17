"""Parser interface and registry.

Add a new catalog platform by creating a module in this package with a @register'd subclass of
CatalogAdapter. Nothing else in the pipeline or database needs to change.
"""
from typing import Dict, List, Optional, Tuple, Type

from ..models import CourseData, CurriculumEntry, ProgramData

DETECT_THRESHOLD = 0.5

_REGISTRY: Dict[str, Type["CatalogAdapter"]] = {}


class CatalogAdapter:
    name: str = ""
    version: int = 1
    # Website domains this parser is known to handle (exact host or suffix match).
    domains: Tuple[str, ...] = ()

    def detect(self, url: str, html: str) -> float:
        """How confident this parser is that it can read the page, from 0 to 1."""
        raise NotImplementedError

    def parse_program(self, url: str, html: str) -> ProgramData:
        raise NotImplementedError

    def course_detail_url(self, program_url: str, entry: CurriculumEntry) -> Optional[str]:
        """Where to fetch the full details for one course (None if the program page already has them)."""
        return entry.detail_url

    def parse_course(self, entry: CurriculumEntry, url: str, html: str) -> CourseData:
        raise NotImplementedError


def register(cls: Type[CatalogAdapter]) -> Type[CatalogAdapter]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} needs a name")
    _REGISTRY[cls.name] = cls
    return cls


def all_adapters() -> List[CatalogAdapter]:
    return [cls() for cls in _REGISTRY.values()]


def get_adapter(name: str) -> CatalogAdapter:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown parser '{name}'. Known: {', '.join(sorted(_REGISTRY))}")
    return _REGISTRY[name]()


def select_adapter(url: str, html: str, override: Optional[str] = None) -> Tuple[Optional[CatalogAdapter], str]:
    """Pick a parser: manual override, then known domain, then the best detect() score."""
    from urllib.parse import urlparse

    if override:
        return get_adapter(override), "override"
    host = urlparse(url).netloc.lower()
    for a in all_adapters():
        if any(host == d or host.endswith("." + d) for d in a.domains):
            return a, "domain"
    scored = sorted(((a.detect(url, html), a) for a in all_adapters()), key=lambda t: t[0], reverse=True)
    if scored and scored[0][0] >= DETECT_THRESHOLD:
        return scored[0][1], f"detected ({scored[0][0]:.2f})"
    return None, "no parser matched"
