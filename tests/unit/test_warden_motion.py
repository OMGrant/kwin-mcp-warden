"""The corner notice's motion follows Plasma's animation speed (kdeglobals)."""

from __future__ import annotations

import pytest

from kwin_mcp.warden.daemon import motion_factor


@pytest.mark.parametrize(
    ("text", "factor"),
    [
        (None, 1.0),
        ("[KDE]\nAnimationDurationFactor=0\n", 0.0),
        ("[KDE]\nAnimationDurationFactor=0.5\n", 0.5),
        ("[General]\nName=x\n[KDE]\nLookAndFeelPackage=org.kde.breezedark.desktop\n", 1.0),
        ("[KDE]\nAnimationDurationFactor=fast\n", 1.0),
        ("[KDE]\nAnimationDurationFactor=-3\n", 0.0),
        ("not a config file at all\n", 1.0),
    ],
)
def test_motion_factor(tmp_path, monkeypatch, text, factor):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if text is not None:
        (tmp_path / "kdeglobals").write_text(text, encoding="utf-8")
    assert motion_factor() == factor
