#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Line icons for the remote and tablet controls.

The controls used to carry Unicode symbols (☀ ☾ ▮ ⏻ ◄◄) as their pictures.
Each one comes from a different font, so they rendered at different sizes and
weights, some fell back to coloured emoji, and the pad looked improvised next
to a real remote.  These are drawn as SVG on one 24-unit grid with one stroke
width, so every key reads as part of the same set.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


ICON_COLOR = "#DCE6F5"
ICON_SIZE = QSize(18, 18)

# 24x24, stroke-only.  Kept as path data so a new key is one line here.
_PATHS = {
    "back": '<path d="M15 18l-6-6 6-6"/><path d="M9 12h11"/>',
    "home": '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
    "recents": '<rect x="4" y="7" width="13" height="13" rx="2"/><path d="M8 4h10a2 2 0 0 1 2 2v10"/>',
    "ok": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.5"/>',
    "menu": '<path d="M4 7h16"/><path d="M4 12h16"/><path d="M4 17h16"/>',
    "power": '<path d="M12 3v9"/><path d="M6.3 6.6a8 8 0 1 0 11.4 0"/>',
    "wake": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2'
            'M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "sleep": '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "mute": '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M17 9l5 6M22 9l-5 6"/>',
    "speaker": '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M17 8.5a5 5 0 0 1 0 7"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "play": '<path d="M7 5l11 7-11 7z"/>',
    "previous": '<path d="M18 6l-8 6 8 6z"/><path d="M6 6v12"/>',
    "next": '<path d="M6 6l8 6-8 6z"/><path d="M18 6v12"/>',
    "screenshot": '<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3'
                  'M8 20H5a1 1 0 0 1-1-1v-3"/><circle cx="12" cy="12" r="3"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1'
                'M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
    "battery": '<rect x="3" y="7" width="16" height="10" rx="2"/><path d="M22 11v2"/><path d="M6 10v4M9 10v4"/>',
    "rotate": '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v5h-5"/>',
    "auto_rotate": '<rect x="7" y="3" width="10" height="18" rx="2"/><path d="M3 9a9 9 0 0 1 3-4"/>'
                   '<path d="M21 15a9 9 0 0 1-3 4"/><path d="M11 18h2"/>',
    "ring": '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
    "find": '<path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',
}

_cache: dict[tuple[str, str], QIcon] = {}


def _svg(name: str, color: str) -> bytes:
    body = _PATHS[name]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="1.9" stroke-linecap="round" '
        f'stroke-linejoin="round">{body}</svg>'
    ).encode("ascii")


def icon(name: str, color: str = ICON_COLOR) -> QIcon:
    """A crisp icon at 1x, 2x and 3x so it stays sharp at any display scale."""
    key = (name, color)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    renderer = QSvgRenderer(QByteArray(_svg(name, color)))
    result = QIcon()
    for scale in (1, 2, 3):
        side = ICON_SIZE.width() * scale
        pixmap = QPixmap(side, side)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, side, side))
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        result.addPixmap(pixmap)
    _cache[key] = result
    return result


def names() -> list[str]:
    return sorted(_PATHS)
