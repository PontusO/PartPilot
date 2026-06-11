"""Core platform: feature registry, database/migrations and shared request deps.

The core owns the cross-cutting concerns every feature needs (auth, navigation,
the SQLite source of truth, settings) and exposes a small extension surface so new
functionality is added as bounded *feature modules* rather than edits to a monolith.
"""

from .refs import ref_no
from .registry import Feature, FeatureRegistry, Migration, NavItem
from .workdays import add_workdays, iso, parse_date, sub_workdays, workdays_between

__all__ = ["Feature", "FeatureRegistry", "Migration", "NavItem", "ref_no",
           "add_workdays", "sub_workdays", "workdays_between", "parse_date", "iso"]
