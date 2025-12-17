from pathlib import Path

from PIL import Image


def test_icon_contains_required_sizes():
    icon_path = Path(__file__).resolve().parents[1] / "src" / "assets" / "icon.ico"
    assert icon_path.exists(), f"icon not found at {icon_path}"

    icon = Image.open(icon_path)
    sizes = set(icon.ico.sizes())

    required = {
        (16, 16),
        (24, 24),
        (32, 32),
        (48, 48),
        (64, 64),
        (128, 128),
        (256, 256),
    }

    missing = required - sizes
    assert not missing, f"icon.ico missing sizes: {sorted(missing)}"
