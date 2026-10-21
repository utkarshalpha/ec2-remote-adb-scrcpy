#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The Usage card in the left pane: Task Manager's Performance tab, compacted.

One metric is shown large at the top -- its figure, a two-minute graph and the
numbers behind it -- and every metric is listed underneath with a small graph
of its own, so a spike on one is visible while another is selected. Clicking a
row (or arrowing to it) brings it to the top.

Capacity is shown as occupied and available side by side, never one alone: an
operator deciding whether a box can take another APK needs both.

The widgets only display; sampling and the ADB call live in the window.
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QLinearGradient, QPainter, QPainterPath, QPen,
)
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)

import cdc_qt as v1
import cdc_usage as usage


CPU_COLOR = "#64A2FF"
GPU_COLOR = "#5ED0E2"
MEMORY_COLOR = "#B48CFF"
STORAGE_COLOR = "#43D6A3"
NETWORK_COLOR = "#F2A65E"

# key, name, colour -- in the order Task Manager lists them.
METRICS = (
    ("cpu", "CPU", CPU_COLOR),
    ("gpu", "GPU", GPU_COLOR),
    ("memory", "Memory", MEMORY_COLOR),
    ("storage", "Storage", STORAGE_COLOR),
    ("network", "Network", NETWORK_COLOR),
)
METRIC_KEYS = tuple(key for key, _name, _color in METRICS)
_METRIC_BY_KEY = {key: (name, color) for key, name, color in METRICS}

# Network has no natural ceiling; the graph scales to what it has seen, but
# never below this, so an idle link does not draw noise as a mountain.
NETWORK_SCALE_FLOOR = 100_000.0     # 100 Kbps

USAGE_STYLESHEET = f"""
QFrame#UsageList {{
    background: {v1.BG_ALT}; border: 1px solid {v1.BORDER}; border-radius: 8px;
}}
QFrame#UsageRow {{ background: transparent; border: 1px solid transparent; border-radius: 6px; }}
QFrame#UsageRow:hover {{ background: {v1.SURFACE_2}; }}
QFrame#UsageRow[selected="true"] {{ background: {v1.SURFACE_3}; }}
QFrame#UsageRow:focus {{ border: 1px solid {v1.BLUE}; }}
QFrame#UsageSeparator {{ background: #1A2840; border: 0; }}
QLabel#UsageRowName {{ font-size: 12px; font-weight: 600; color: {v1.TEXT}; }}
QLabel#UsageRowValue {{ font-size: 11px; color: {v1.MUTED}; }}
QLabel#UsageDetailTitle {{ font-size: 15px; font-weight: 700; color: {v1.TEXT}; }}
QLabel#UsageBig {{
    font-family: "Segoe UI Variable Display", "Segoe UI";
    font-size: 26px; font-weight: 600;
}}
QLabel#UsageSubtitle {{ font-size: 11px; color: {v1.MUTED}; }}
QLabel#UsageCaption {{ font-size: 10px; color: {v1.SUBTLE}; }}
QLabel#UsageLegend {{ font-size: 10px; color: {v1.MUTED}; }}
QLabel#UsageStat {{ font-size: 12px; font-weight: 600; color: {v1.TEXT}; }}
QLabel#UsageStatus {{
    font-size: 10px; font-weight: 700; color: {v1.MUTED};
    background: {v1.SURFACE_2}; border: 1px solid {v1.BORDER};
    border-radius: 9px; padding: 2px 8px;
}}
QLabel#UsageStatus[tone="online"] {{ color: {v1.GREEN}; border-color: #245B50; }}
QLabel#UsageStatus[tone="pending"] {{ color: {v1.AMBER}; border-color: #5B4C27; }}
QLabel#UsageStatus[tone="error"] {{ color: {v1.RED}; border-color: #633044; }}
QToolButton#UsageClose {{
    background: transparent; border: 0; border-radius: 11px; color: {v1.SUBTLE};
    min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
    font-size: 16px; padding-bottom: 2px;
}}
QToolButton#UsageClose:hover {{ background: {v1.SURFACE_2}; color: {v1.TEXT}; }}
"""


def _repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _label(text, name):
    label = QLabel(text)
    label.setObjectName(name)
    return label


def _shrinkable(label):
    """Let a long value clip rather than force the sidebar wider."""
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    label.setMinimumWidth(0)
    return label


# ------------------------------------------------------------------ graphs --
class UsageGraph(QWidget):
    """A line with a soft fill, newest sample on the right edge."""

    def __init__(self, color, compact=False, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._compact = compact
        self._primary = []
        self._secondary = None
        self._maximum = 100.0
        self._message = ""
        if compact:
            self.setFixedSize(56, 30)
        else:
            self.setMinimumHeight(96)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def sizeHint(self):
        return QSize(56, 30) if self._compact else QSize(220, 96)

    def set_color(self, color):
        self._color = QColor(color)
        self.update()

    def set_series(self, primary, secondary=None, maximum=100.0, message=""):
        self._primary = list(primary)
        self._secondary = list(secondary) if secondary is not None else None
        self._maximum = maximum if maximum and maximum > 0 else 100.0
        self._message = message
        self.update()

    def has_data(self):
        return any(value is not None for value in self._primary)

    def _points(self, values, plot):
        step = plot.width() / max(1, usage.HISTORY_LENGTH - 1)
        offset = usage.HISTORY_LENGTH - len(values)
        runs, run = [], []
        for index, value in enumerate(values):
            if value is None:
                if run:
                    runs.append(run)
                run = []
                continue
            fraction = max(0.0, min(1.0, value / self._maximum))
            x = plot.left() + (offset + index) * step
            y = plot.bottom() - fraction * plot.height()
            run.append((x, y))
        if run:
            runs.append(run)
        return runs

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 4.0 if self._compact else 6.0
        painter.setPen(QPen(QColor(v1.BORDER), 1) if not self._compact else Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#09101C"))
        painter.drawRoundedRect(frame, radius, radius)

        plot = frame.adjusted(1.5, 2.5, -1.5, -1.5)
        if not self._compact:
            grid = QPen(QColor(255, 255, 255, 14), 1)
            painter.setPen(grid)
            for row in range(1, 4):
                y = plot.top() + plot.height() * row / 4
                painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            for column in range(1, 6):
                x = plot.left() + plot.width() * column / 6
                painter.drawLine(int(x), int(plot.top()), int(x), int(plot.bottom()))

        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(frame, radius, radius)
        painter.setClipPath(clip)
        for points in self._points(self._primary, plot):
            self._draw_run(painter, points, plot, filled=True)
        if self._secondary is not None:
            for points in self._points(self._secondary, plot):
                self._draw_run(painter, points, plot, filled=False)
        painter.restore()

        if self._message and not self.has_data() and not self._compact:
            painter.setPen(QColor(v1.SUBTLE))
            font = painter.font()
            font.setPixelSize(11)
            painter.setFont(font)
            painter.drawText(
                plot.adjusted(10, 0, -10, 0),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._message)
        painter.end()

    def _draw_run(self, painter, points, plot, filled):
        path = QPainterPath()
        path.moveTo(*points[0])
        for point in points[1:]:
            path.lineTo(*point)
        if len(points) == 1:
            # A single sample is still a reading; show it as a short tick.
            path.lineTo(points[0][0] - 2, points[0][1])
        if filled:
            area = QPainterPath(path)
            area.lineTo(points[-1][0], plot.bottom())
            area.lineTo(points[0][0], plot.bottom())
            area.closeSubpath()
            gradient = QLinearGradient(0, plot.top(), 0, plot.bottom())
            top = QColor(self._color)
            top.setAlpha(96 if not self._compact else 80)
            bottom = QColor(self._color)
            bottom.setAlpha(8)
            gradient.setColorAt(0.0, top)
            gradient.setColorAt(1.0, bottom)
            painter.fillPath(area, gradient)
            pen = QPen(self._color, 1.6 if not self._compact else 1.2)
        else:
            lighter = QColor(self._color).lighter(135)
            pen = QPen(lighter, 1.3)
            pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)


class CapacityBar(QWidget):
    """Occupied and available in one bar, occupied from the left."""

    def __init__(self, color, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._fraction = None
        self.setFixedHeight(8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, fraction, color=None):
        if color is not None:
            self._color = QColor(color)
        self._fraction = None if fraction is None else max(0.0, min(1.0, fraction))
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(v1.SURFACE_3))
        painter.drawRoundedRect(track, 4, 4)
        if self._fraction:
            used = QRectF(track)
            used.setWidth(max(track.height(), track.width() * self._fraction))
            painter.setBrush(self._color)
            painter.drawRoundedRect(used, 4, 4)
        painter.end()


class CoreStrip(QWidget):
    """One bar per CPU core, so a single pegged core is not averaged away."""

    def __init__(self, color, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._values = []
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, values):
        self._values = list(values)
        self.update()

    def paintEvent(self, _event):
        if not self._values:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        count = len(self._values)
        gap = 3.0
        width = (self.width() - gap * (count - 1)) / count
        painter.setPen(Qt.PenStyle.NoPen)
        for index, value in enumerate(self._values):
            x = index * (width + gap)
            track = QRectF(x, 0, width, self.height())
            painter.setBrush(QColor(v1.SURFACE_3))
            painter.drawRoundedRect(track, 2.5, 2.5)
            fraction = max(0.0, min(1.0, (value or 0.0) / 100.0))
            if fraction > 0:
                height = max(2.0, track.height() * fraction)
                fill = QRectF(x, track.bottom() - height, width, height)
                painter.setBrush(self._color)
                painter.drawRoundedRect(fill, 2.5, 2.5)
        painter.end()


class LegendItem(QWidget):
    """A colour swatch beside a short label, for graphs with two series."""

    def __init__(self, color, text="", dashed=False, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.swatch = QLabel()
        self.swatch.setFixedSize(10, 3 if dashed else 8)
        self.text = _label(text, "UsageLegend")
        layout.addWidget(self.swatch, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.text)
        self.set_color(color, dashed)

    def set_color(self, color, dashed=False):
        shade = QColor(color).lighter(135).name() if dashed else color
        style = (f"background: {shade}; border-radius: 1px;" if dashed
                 else f"background: {color}; border-radius: 2px;")
        self.swatch.setFixedSize(10, 3 if dashed else 8)
        self.swatch.setStyleSheet(style)


# -------------------------------------------------------------------- rows --
class MetricRow(QFrame):
    """One line of the list: a small graph, the name, and the current value."""

    activated = Signal(str)
    navigate = Signal(str, int)

    def __init__(self, key, name, color, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("UsageRow")
        self.setProperty("selected", False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(name)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 5, 8, 5)
        layout.setSpacing(9)
        self.graph = UsageGraph(color, compact=True)
        layout.addWidget(self.graph)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        self.name_label = _label(name, "UsageRowName")
        self.value_label = _shrinkable(_label("—", "UsageRowValue"))
        text.addWidget(self.name_label)
        text.addWidget(self.value_label)
        layout.addLayout(text, 1)

    def set_selected(self, selected):
        if self.property("selected") != selected:
            self.setProperty("selected", selected)
            _repolish(self)

    def set_value_text(self, text):
        self.value_label.setText(text)
        self.setAccessibleDescription(text)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.activated.emit(self.key)
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.key)
        elif key == Qt.Key.Key_Up:
            self.navigate.emit(self.key, -1)
        elif key == Qt.Key.Key_Down:
            self.navigate.emit(self.key, 1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()


# ------------------------------------------------------------------- panel --
class UsagePanel(QFrame):
    """The whole card. Feed it samples with ``add_sample``."""

    close_requested = Signal()
    metric_changed = Signal(str)

    def __init__(self, interval_seconds=2, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self._interval = interval_seconds
        self._selected = "cpu"
        self._latest = None
        self._message = "Connect a device to see live usage"
        self._history = {
            key: deque(maxlen=usage.HISTORY_LENGTH) for key in METRIC_KEYS}
        self._history["network_tx"] = deque(maxlen=usage.HISTORY_LENGTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 10, 11, 11)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(_label("Usage", "CardTitle"))
        header.addStretch(1)
        self.status_label = _label("No device", "UsageStatus")
        self.status_label.setProperty("tone", "idle")
        header.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.close_button = QToolButton()
        self.close_button.setObjectName("UsageClose")
        self.close_button.setText("×")
        self.close_button.setToolTip("Hide usage (Ctrl+U)")
        self.close_button.setAccessibleName("Hide usage")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.close_requested.emit)
        header.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)

        layout.addLayout(self._build_detail())
        layout.addWidget(self._build_list())
        self._render()

    # ---------- construction ----------
    def _build_detail(self):
        detail = QVBoxLayout()
        detail.setSpacing(6)

        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.detail_title = _label("CPU", "UsageDetailTitle")
        self.detail_subtitle = _shrinkable(_label("", "UsageSubtitle"))
        text.addWidget(self.detail_title)
        text.addWidget(self.detail_subtitle)
        title_row.addLayout(text, 1)
        self.big_value = _label("—", "UsageBig")
        self.big_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self.big_value, 0, Qt.AlignmentFlag.AlignBottom)
        detail.addLayout(title_row)

        captions_top = QHBoxLayout()
        self.caption_left = _label("", "UsageCaption")
        self.caption_right = _label("", "UsageCaption")
        captions_top.addWidget(self.caption_left)
        captions_top.addStretch(1)
        captions_top.addWidget(self.caption_right)
        detail.addLayout(captions_top)

        self.detail_graph = UsageGraph(CPU_COLOR)
        detail.addWidget(self.detail_graph)

        captions_bottom = QHBoxLayout()
        minutes = usage.HISTORY_LENGTH * self._interval / 60
        span = f"{minutes:g} min" if minutes >= 1 else f"{usage.HISTORY_LENGTH * self._interval} s"
        captions_bottom.addWidget(_label(span, "UsageCaption"))
        captions_bottom.addStretch(1)
        captions_bottom.addWidget(_label("now", "UsageCaption"))
        detail.addLayout(captions_bottom)

        legend = QHBoxLayout()
        legend.setSpacing(12)
        self.legend_primary = LegendItem(CPU_COLOR)
        self.legend_secondary = LegendItem(CPU_COLOR, dashed=True)
        legend.addWidget(self.legend_primary)
        legend.addWidget(self.legend_secondary)
        legend.addStretch(1)
        self.legend_row = QWidget()
        self.legend_row.setLayout(legend)
        legend.setContentsMargins(0, 2, 0, 0)
        detail.addWidget(self.legend_row)

        self.capacity_bar = CapacityBar(MEMORY_COLOR)
        detail.addWidget(self.capacity_bar)
        self.core_strip = CoreStrip(CPU_COLOR)
        detail.addWidget(self.core_strip)

        grid = QGridLayout()
        grid.setContentsMargins(0, 4, 0, 2)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(7)
        self._stat_cells = []
        for index in range(6):
            cell = QVBoxLayout()
            cell.setSpacing(1)
            name = _label("", "FieldLabel")
            value = _shrinkable(_label("", "UsageStat"))
            cell.addWidget(name)
            cell.addWidget(value)
            grid.addLayout(cell, index // 2, index % 2)
            self._stat_cells.append((name, value))
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        detail.addLayout(grid)
        return detail

    def _build_list(self):
        frame = QFrame()
        frame.setObjectName("UsageList")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(0)
        self.rows = {}
        for index, (key, name, color) in enumerate(METRICS):
            if index:
                separator = QFrame()
                separator.setObjectName("UsageSeparator")
                separator.setFixedHeight(1)
                separator.setContentsMargins(0, 0, 0, 0)
                wrap = QHBoxLayout()
                # Indented past the small graph, as HIG grouped lists do.
                wrap.setContentsMargins(71, 1, 6, 1)
                wrap.addWidget(separator)
                layout.addLayout(wrap)
            row = MetricRow(key, name, color)
            row.activated.connect(self.select)
            row.navigate.connect(self._navigate)
            layout.addWidget(row)
            self.rows[key] = row
        return frame

    # ---------- public ----------
    def selected_metric(self):
        return self._selected

    def has_samples(self):
        return self._latest is not None

    def select(self, key):
        if key not in _METRIC_BY_KEY:
            return
        changed = key != self._selected
        self._selected = key
        self._render()
        if changed:
            self.metric_changed.emit(key)

    def set_status(self, text, tone="idle"):
        self.status_label.setText(text)
        if self.status_label.property("tone") != tone:
            self.status_label.setProperty("tone", tone)
            _repolish(self.status_label)

    def clear(self, message="Connect a device to see live usage"):
        """Forget everything: a different device, or none."""
        for history in self._history.values():
            history.clear()
        self._latest = None
        self._dropped = False
        self._message = message
        self._render()

    def drop_to_zero(self, message="Device disconnected"):
        """The device went away: every live reading falls to zero, then holds.

        Clearing instead made the panel look frozen on its last values until
        the next device arrived, which read as the device still being busy.
        Capacities that do not stop existing -- installed memory, disk size --
        keep their totals so the rows still say what was measured.
        """
        last = self._latest
        if last is None or getattr(self, "_dropped", False):
            self._message = message
            self._render()
            return
        zero = usage.UsageSample(
            serial=last.serial,
            cpu_percent=0.0,
            core_percents=[0.0] * len(last.core_percents),
            cpu_cores=last.cpu_cores,
            gpu_percent=0.0 if last.gpu_percent is not None else None,
            mem_total=last.mem_total,
            mem_used=0 if last.mem_total else None,
            mem_available=last.mem_total,
            storage_total=last.storage_total,
            storage_used=last.storage_used,
            storage_free=last.storage_free,
            storage_mount=last.storage_mount,
            disk_active_percent=0.0,
            disk_read_bps=0.0,
            disk_write_bps=0.0,
            net_rx_bps=0.0,
            net_tx_bps=0.0,
            net_interface=last.net_interface,
        )
        self.add_sample(zero)
        self._dropped = True
        self._message = message

    def add_sample(self, sample):
        self._dropped = False
        self._latest = sample
        self._message = ""
        history = self._history
        history["cpu"].append(sample.cpu_percent)
        history["gpu"].append(sample.gpu_percent)
        history["memory"].append(sample.mem_percent)
        history["storage"].append(sample.disk_active_percent)
        history["network"].append(sample.net_rx_bps)
        history["network_tx"].append(sample.net_tx_bps)
        self._render()

    # ---------- drawing ----------
    def _network_scale(self):
        seen = [value for key in ("network", "network_tx")
                for value in self._history[key] if value is not None]
        return usage.nice_ceiling(max(seen, default=0.0), NETWORK_SCALE_FLOOR)

    def _navigate(self, key, step):
        index = (METRIC_KEYS.index(key) + step) % len(METRIC_KEYS)
        target = METRIC_KEYS[index]
        self.rows[target].setFocus(Qt.FocusReason.TabFocusReason)
        self.select(target)

    def _render(self):
        sample = self._latest
        network_scale = self._network_scale()
        for key, row in self.rows.items():
            row.set_selected(key == self._selected)
            if key == "network":
                row.graph.set_series(
                    self._history["network"], self._history["network_tx"],
                    network_scale)
            else:
                row.graph.set_series(self._history[key])
            row.set_value_text(self._row_text(key, sample))
        self._render_detail(sample, network_scale)

    @staticmethod
    def _row_text(key, s):
        if s is None:
            return "—"
        if key == "cpu":
            if s.cpu_percent is None:
                return "Measuring…"
            if s.cpu_freq_mhz is not None:
                return f"{usage.format_percent(s.cpu_percent)}  {usage.format_clock(s.cpu_freq_mhz)}"
            return usage.format_percent(s.cpu_percent)
        if key == "gpu":
            if s.gpu_percent is None:
                return "Not reported"
            return usage.format_percent(s.gpu_percent)
        if key == "memory":
            if s.mem_total is None:
                return "Not reported"
            return (f"{usage.format_bytes(s.mem_used)} / "
                    f"{usage.format_bytes(s.mem_total)} "
                    f"({usage.format_percent(s.mem_percent)})")
        if key == "storage":
            if s.storage_total is None:
                return "Not reported"
            return (f"{usage.format_percent(s.storage_percent)} full · "
                    f"{usage.format_bytes(s.storage_free)} free")
        if key == "network":
            if s.net_rx_bps is None:
                return "Measuring…" if s.net_rx_total is not None else "Not reported"
            return (f"↓ {usage.format_bits(s.net_rx_bps)}  "
                    f"↑ {usage.format_bits(s.net_tx_bps)}")
        return "—"

    def _set_stats(self, pairs):
        for index, (name_label, value_label) in enumerate(self._stat_cells):
            if index < len(pairs):
                name, value = pairs[index]
                name_label.setText(name.upper())
                value_label.setText(value)
                name_label.show()
                value_label.show()
            else:
                name_label.hide()
                value_label.hide()

    def _render_detail(self, s, network_scale):
        key = self._selected
        name, color = _METRIC_BY_KEY[key]
        self.detail_title.setText(name)
        self.big_value.setStyleSheet(f"color: {color};")
        self.detail_graph.set_color(color)
        self.capacity_bar.set_value(None, color)
        self.core_strip._color = QColor(color)
        f = usage
        waiting = self._message or "Collecting the first readings…"

        show_legend = False
        show_capacity = False
        show_cores = False
        secondary = None
        maximum = 100.0
        top_left, top_right = "% utilisation", "100%"

        if key == "cpu":
            big = f.format_percent(s.cpu_percent if s else None)
            subtitle = ""
            if s:
                parts = []
                if s.cpu_freq_mhz is not None:
                    parts.append(f.format_clock(s.cpu_freq_mhz))
                if s.cpu_cores:
                    parts.append(f"{s.cpu_cores} cores")
                subtitle = " · ".join(parts)
            stats = [
                ("Speed", f.format_clock(s.cpu_freq_mhz if s else None)),
                ("Cores", str(s.cpu_cores) if s and s.cpu_cores else "—"),
                ("Temperature", f.format_temperature(s.cpu_temp_c if s else None)),
                ("Up time", f.format_uptime(s.uptime_s if s else None)),
            ]
            show_cores = bool(s and s.core_percents)
            if show_cores:
                self.core_strip.set_values(s.core_percents)
            message = waiting
        elif key == "gpu":
            reported = bool(s and s.gpu_percent is not None)
            big = f.format_percent(s.gpu_percent if s else None)
            subtitle = (f.format_clock(s.gpu_freq_mhz) if reported and s.gpu_freq_mhz
                        else ("" if reported or s is None else "Not reported by this device"))
            stats = [
                ("Clock", f.format_clock(s.gpu_freq_mhz if s else None)),
                ("Temperature", f.format_temperature(s.gpu_temp_c if s else None)),
            ]
            message = (waiting if s is None else
                       "This device does not report GPU load over ADB")
        elif key == "memory":
            big = f.format_percent(s.mem_percent if s else None)
            subtitle = (f"{f.format_bytes(s.mem_used)} of {f.format_bytes(s.mem_total)} in use"
                        if s and s.mem_total else "")
            top_left = "Memory in use"
            # In use and available are on the legend beside the bar.
            stats = [
                ("Cached", f.format_bytes(s.mem_cached if s else None)),
                ("Total", f.format_bytes(s.mem_total if s else None)),
            ]
            if s and s.mem_total:
                show_legend = show_capacity = True
                self.capacity_bar.set_value(s.mem_used / s.mem_total, color)
                self._set_legend(color, f"In use {f.format_bytes(s.mem_used)}",
                                 v1.SURFACE_3, f"Available {f.format_bytes(s.mem_available)}")
            message = waiting
        elif key == "storage":
            big = f.format_percent(s.storage_percent if s else None)
            subtitle = (f"{f.format_bytes(s.storage_free)} free of {f.format_bytes(s.storage_total)}"
                        if s and s.storage_total else "")
            top_left = "Active time"
            # Used and free are on the legend beside the bar.
            stats = [
                ("Read", f.format_byte_rate(s.disk_read_bps if s else None)),
                ("Write", f.format_byte_rate(s.disk_write_bps if s else None)),
                ("Active time", f.format_percent(s.disk_active_percent if s else None)),
                ("Total", f.format_bytes(s.storage_total if s else None)),
            ]
            if s and s.storage_percent is not None:
                show_legend = show_capacity = True
                self.capacity_bar.set_value(s.storage_percent / 100.0, color)
                self._set_legend(color, f"Used {f.format_bytes(s.storage_used)}",
                                 v1.SURFACE_3, f"Free {f.format_bytes(s.storage_free)}")
            message = waiting
        else:  # network
            big = f.format_bits(s.net_rx_bps if s else None)
            subtitle = f"Receiving on {s.net_interface}" if s and s.net_interface else ""
            top_left, top_right = "Throughput", f.format_bits(network_scale)
            secondary = self._history["network_tx"]
            maximum = network_scale
            stats = [
                ("Receive", f.format_bits(s.net_rx_bps if s else None)),
                ("Send", f.format_bits(s.net_tx_bps if s else None)),
                ("Received", f.format_bytes(s.net_rx_total if s else None)),
                ("Sent", f.format_bytes(s.net_tx_total if s else None)),
            ]
            if s:
                show_legend = True
                self._set_legend(color, "Receive", color, "Send", dashed=True)
            message = waiting

        self.big_value.setText(big)
        self.detail_subtitle.setText(subtitle)
        self.caption_left.setText(top_left)
        self.caption_right.setText(top_right)
        self.detail_graph.set_series(
            self._history[key], secondary, maximum, message)
        self.legend_row.setVisible(show_legend)
        self.capacity_bar.setVisible(show_capacity)
        self.core_strip.setVisible(show_cores)
        self._set_stats(stats)

    def _set_legend(self, first_color, first_text, second_color, second_text, dashed=False):
        self.legend_primary.set_color(first_color)
        self.legend_primary.text.setText(first_text)
        self.legend_secondary.set_color(second_color, dashed=dashed)
        self.legend_secondary.text.setText(second_text)
