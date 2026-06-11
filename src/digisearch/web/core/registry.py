"""Feature descriptors and the registry that wires them into the app.

A *feature* is a self-contained module (e.g. purchasing, catalog, inventory) that declares
what it contributes to the platform: web routes, a nav entry, the database tables it
owns, and any roles it introduces. The core includes its router, runs its migrations,
and shows its nav entry to the right roles — the feature never edits the core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from fastapi import APIRouter


@dataclass(frozen=True)
class NavItem:
    """A top-nav entry. ``roles=None`` means visible to every signed-in user."""

    label: str
    url: str
    roles: frozenset[str] | None = None
    icon: str = ""
    order: int = 100
    placeholder: bool = False  # a not-yet-built category, shown greyed with a "soon" tag

    def visible_to(self, role: str | None) -> bool:
        if self.roles is None:
            return True
        return role is not None and role in self.roles


@dataclass(frozen=True)
class Migration:
    """One ordered schema change owned by a feature. SQL may hold several statements."""

    version: int
    name: str
    sql: str


@dataclass
class Feature:
    """Everything one feature module contributes to the platform."""

    name: str
    router: APIRouter | None = None
    nav: NavItem | None = None
    migrations: list[Migration] = field(default_factory=list)
    roles: tuple[str, ...] = ()  # extra roles this feature introduces
    template_dir: Path | None = None


class FeatureRegistry:
    """Collects features and answers the questions the core asks of them."""

    def __init__(self) -> None:
        self._features: list[Feature] = []

    def register(self, *features: Feature) -> None:
        for feature in features:
            if any(f.name == feature.name for f in self._features):
                raise ValueError(f"duplicate feature {feature.name!r}")
            self._features.append(feature)

    @property
    def features(self) -> list[Feature]:
        return list(self._features)

    def template_dirs(self) -> list[Path]:
        return [f.template_dir for f in self._features if f.template_dir]

    def nav_for(self, role: str | None) -> list[NavItem]:
        items = [f.nav for f in self._features if f.nav and f.nav.visible_to(role)]
        return sorted(items, key=lambda n: (n.order, n.label))

    def all_migrations(self) -> Iterator[tuple[str, Migration]]:
        for feature in self._features:
            for mig in sorted(feature.migrations, key=lambda m: m.version):
                yield feature.name, mig

    def extra_roles(self) -> Iterable[str]:
        for feature in self._features:
            yield from feature.roles
