"""The Feature.background_tasks seam: features declare their loops, the core spawns whatever is
declared (app.py's lifespan iterates the registry instead of importing feature internals)."""

from digisearch.web.app import FEATURES
from digisearch.web.features.catalog.devmgmt_sync import devmgmt_sync_loop
from digisearch.web.features.catalog.feature import feature as catalog_feature
from digisearch.web.features.setup.feature import feature as setup_feature
from digisearch.web.features.setup.scheduler import webshop_sync_loop


def test_features_declare_their_background_loops():
    assert devmgmt_sync_loop in catalog_feature.background_tasks
    assert webshop_sync_loop in setup_feature.background_tasks


def test_only_catalog_and_setup_declare_loops():
    assert {f.name for f in FEATURES if f.background_tasks} == {"catalog", "setup"}
