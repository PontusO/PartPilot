from pathlib import Path

from digisearch.config import Settings


def test_settings_defaults():
    s = Settings()
    assert s.minimrp_path is None
    assert s.build_qty == 1
    assert s.reel_threshold == 10000.0


def test_settings_load_operational_defaults(tmp_path: Path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(
        "minimrp_path: /data/mrp5data\n"
        "build_qty: 100\n"
        "currency: SEK\n"
        "reel_threshold: 5000\n"
        "confidence_threshold: 0.8\n"
    )
    s = Settings.load(cfg)
    assert s.minimrp_path == "/data/mrp5data"
    assert s.build_qty == 100
    assert s.currency == "SEK"
    assert s.reel_threshold == 5000
    assert s.confidence_threshold == 0.8


def test_settings_load_missing_file_uses_defaults(tmp_path: Path):
    s = Settings.load(tmp_path / "nope.yaml")
    assert s.build_qty == 1 and s.minimrp_path is None
