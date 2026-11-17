"""Catalog parsers. Importing this package registers every parser module listed below."""
from .base import CatalogAdapter, all_adapters, get_adapter, register, select_adapter  # noqa: F401
from . import acalog, courseleaf, smartcatalog  # noqa: F401  (registers parsers)
