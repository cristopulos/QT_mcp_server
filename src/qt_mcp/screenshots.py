"""Cross-platform screenshot capture for Qt applications (and any desktop window).

OS-level capture strategy (no Qt app modification required):
  - **Linux/X11**: enumerate windows via ``wmctrl -l`` / ``xwininfo``.
  - **Windows**: enumerate windows via Win32 ``EnumWindows`` (ctypes, no
    third-party dependencies).
  - Match by (partial) window title.
  - Grab the pixel region with ``mss`` (fast screen-grab) and encode to PNG
    with Pillow.

A Qt-internal capture hook is stubbed via :func:`capture_widget`, which is
intended to be implemented when a Qt-side agent can be injected.  Until then it
returns a clear "not available" error so callers can fall back to region capture.
"""

from __future__ import annotations

import logging
import sys
import re
import shutil
import subprocess
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

from PIL import Image as PILImage

logger = logging.getLogger(__name__)


class CaptureError(Exception):
    """Raised when a screenshot cannot be captured."""


@dataclass
class WindowInfo:
    """A discovered window."""

    id: str  # hex window id, e.g. "0x0380000a"
    title: str
    desktop: int  # -1 if sticky / unknown
    x: int
    y: int
    width: int
    height: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "desktop": self.desktop,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


def _wmctrl_available() -> bool:
    return shutil.which("wmctrl") is not None


def _xwininfo_available() -> bool:
    return shutil.which("xwininfo") is not None


def _list_windows_win32() -> list[WindowInfo]:
    """Enumerate top-level windows on Windows via Win32 ``EnumWindows`` (ctypes).

    Returns a list of :class:`WindowInfo` with physical-pixel coordinates
    matching ``mss``.
    """
    import ctypes
    import ctypes.wintypes

    # --- DPI awareness ladder (physical pixels) ---
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass

    user32 = ctypes.windll.user32

    # --- Set up argtypes / restype for safety ---
    user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM),
        ctypes.wintypes.LPARAM,
    ]
    user32.EnumWindows.restype = ctypes.c_bool

    user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindowVisible.restype = ctypes.c_bool

    user32.GetWindowTextW.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int

    user32.GetWindowRect.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPRECT]
    user32.GetWindowRect.restype = ctypes.c_bool

    # --- RECT structure ---
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    windows: list[WindowInfo] = []

    # --- Callback ---
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def _enum_callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        length = user32.GetWindowTextW(hwnd, buf, 512)
        if length == 0:
            return True
        title = buf.value
        if not title:
            return True
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return True
        windows.append(
            WindowInfo(
                id=hex(hwnd),
                title=title,
                desktop=-1,
                x=rect.left,
                y=rect.top,
                width=width,
                height=height,
            )
        )
        return True

    cb = WNDENUMPROC(_enum_callback)  # keep reference alive during enumeration
    try:
        if not user32.EnumWindows(cb, 0):
            raise CaptureError("Win32 EnumWindows returned False (failure).")
    except Exception as exc:
        raise CaptureError(f"Win32 enumeration failed: {exc}") from exc
    return windows


def list_windows() -> list[WindowInfo]:
    """List all visible top-level windows on the current display.

    Platform dispatch:
      - **Windows**: uses Win32 ``EnumWindows`` (ctypes).
      - **Linux/X11**: uses ``wmctrl -lG`` (title + geometry), falling back to
        ``xwininfo -root -tree`` if wmctrl is unavailable.
      - Raises :class:`CaptureError` if no suitable method is available.
    """
    if sys.platform == "win32":
        return _list_windows_win32()
    if _wmctrl_available():
        return _list_windows_wmctrl()
    if _xwininfo_available():
        return _list_windows_xwininfo()
    raise CaptureError(
        "No window-listing tool found. Install `wmctrl` (recommended) or "
        "`xwininfo` (part of x11-utils)."
    )


def _list_windows_wmctrl() -> list[WindowInfo]:
    try:
        out = subprocess.run(
            ["wmctrl", "-lG"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise CaptureError(f"wmctrl failed: {exc}") from exc
    if out.returncode != 0:
        raise CaptureError(f"wmctrl failed (rc={out.returncode}): {out.stderr.strip()}")

    windows: list[WindowInfo] = []
    # Format: 0xID  DESKTOP  X Y W H  HOST  TITLE...
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 7)
        if len(parts) < 6:
            continue
        wid = parts[0]
        try:
            desktop = int(parts[1])
        except ValueError:
            desktop = -1
        try:
            gx, gy, gw, gh = (int(v) for v in parts[2:6])
        except ValueError:
            continue
        title = parts[7].strip() if len(parts) > 7 else ""
        windows.append(
            WindowInfo(
                id=wid,
                title=title,
                desktop=desktop,
                x=gx,
                y=gy,
                width=gw,
                height=gh,
            )
        )
    return windows


def _list_windows_xwininfo() -> list[WindowInfo]:
    """Best-effort fallback using xwininfo -root -tree."""
    try:
        out = subprocess.run(
            ["xwininfo", "-root", "-tree"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise CaptureError(f"xwininfo failed: {exc}") from exc
    if out.returncode != 0:
        raise CaptureError(f"xwininfo failed (rc={out.returncode}): {out.stderr.strip()}")

    # Lines look like: 0x0380000a "Window Title": (class)
    windows: list[WindowInfo] = []
    line_re = re.compile(r'^(0x[0-9a-f]+)\s+"([^"]*)":')
    for line in out.stdout.splitlines():
        m = line_re.match(line.strip())
        if not m:
            continue
        wid = m.group(1)
        title = m.group(2)
        # xwininfo tree output lacks geometry per child reliably; resolve via -stats
        geo = _geometry_for_window(wid)
        if geo is None:
            continue
        gx, gy, gw, gh = geo
        windows.append(
            WindowInfo(id=wid, title=title, desktop=-1, x=gx, y=gy, width=gw, height=gh)
        )
    return windows


def _geometry_for_window(wid: str) -> Optional[tuple[int, int, int, int]]:
    try:
        out = subprocess.run(
            ["xwininfo", "-id", wid], capture_output=True, text=True, timeout=10
        )
    except subprocess.SubprocessError:
        return None
    if out.returncode != 0:
        return None
    x = y = w = h = None
    for line in out.stdout.splitlines():
        if "Absolute upper-left X:" in line:
            x = int(line.split(":")[-1].strip())
        elif "Absolute upper-left Y:" in line:
            y = int(line.split(":")[-1].strip())
        elif "Width:" in line:
            w = int(line.split(":")[-1].strip())
        elif "Height:" in line:
            h = int(line.split(":")[-1].strip())
    if None in (x, y, w, h):
        return None
    return x, y, w, h  # type: ignore[return-value]


def find_window(title: str, exact: bool = False) -> WindowInfo:
    """Find a window by (case-insensitive) title substring or exact match.

    Raises :class:`CaptureError` when zero or multiple windows match a non-exact
    query, or when no window matches an exact query.
    """
    windows = list_windows()
    if not windows:
        raise CaptureError("No windows found on the current display.")
    if exact:
        matches = [w for w in windows if w.title == title]
        if not matches:
            raise CaptureError(f"No window with exact title {title!r}.")
        return matches[0]
    needle = title.lower()
    matches = [w for w in windows if needle in w.title.lower()]
    if not matches:
        raise CaptureError(
            f"No window title contains {title!r}. "
            f"Use list_windows to see available windows."
        )
    if len(matches) > 1:
        names = ", ".join(w.title for w in matches[:10])
        raise CaptureError(
            f"{len(matches)} windows match {title!r}: {names}. "
            "Provide a more specific title or use exact=True."
        )
    return matches[0]


def _maybe_import_mss():
    """Import mss lazily; return the module or None if unavailable."""
    try:
        import mss  # type: ignore[import-not-found]
        return mss
    except Exception as exc:  # pragma: no cover - import-time
        logger.debug("mss unavailable (%s); falling back to import", exc)
        return None


def _grab_region(x: int, y: int, width: int, height: int) -> bytes:
    """Grab a screen region and return PNG-encoded bytes.

    Uses ``mss`` for fast X11 capture; falls back to ImageMagick ``import`` if
    mss fails (e.g. no display).
    """
    if width <= 0 or height <= 0:
        raise CaptureError(f"Invalid region size {width}x{height}.")
    mss = _maybe_import_mss()
    if mss is None:
        return _grab_region_import(x, y, width, height)

    monitor = {"top": y, "left": x, "width": width, "height": height}
    try:
        with mss.mss() as sct:
            shot = sct.grab(monitor)
            pil = PILImage.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            buf = BytesIO()
            pil.save(buf, format="PNG")
            return buf.getvalue()
    except Exception as exc:
        logger.debug("mss grab failed (%s); falling back to import", exc)
        return _grab_region_import(x, y, width, height)


def _grab_region_import(x: int, y: int, width: int, height: int) -> bytes:
    """Fallback capture using ImageMagick ``import``."""
    if not shutil.which("import"):
        raise CaptureError(
            "Screen capture failed. Neither `mss` nor ImageMagick `import` worked."
        )
    geom = f"{width}x{height}+{x}+{y}"
    try:
        out = subprocess.run(
            ["import", "-window", "root", "-crop", geom, "png:-"],
            capture_output=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise CaptureError(f"ImageMagick import failed: {exc}") from exc
    if out.returncode != 0 or not out.stdout:
        raise CaptureError(
            f"ImageMagick import failed (rc={out.returncode}): "
            f"{out.stderr.decode(errors='replace').strip()}"
        )
    return out.stdout


def capture_window(title: str, exact: bool = False) -> dict:
    """Capture an entire window identified by title.

    Returns a dict with ``png`` (raw PNG bytes) and ``window`` metadata.
    """
    win = find_window(title, exact=exact)
    png = _grab_region(win.x, win.y, win.width, win.height)
    return {"png": png, "window": win.to_dict()}


def capture_region(
    x: int, y: int, width: int, height: int, title: Optional[str] = None
) -> dict:
    """Capture a rectangular screen region.

    When ``title`` is given the region coordinates are interpreted relative to
    that window's top-left corner (the window is located and the absolute
    screen coords are computed).  Otherwise coordinates are absolute screen
    coordinates.
    """
    if title is not None:
        win = find_window(title)
        abs_x = win.x + int(x)
        abs_y = win.y + int(y)
    else:
        abs_x, abs_y = int(x), int(y)
    png = _grab_region(abs_x, abs_y, int(width), int(height))
    return {
        "png": png,
        "region": {
            "x": abs_x,
            "y": abs_y,
            "width": int(width),
            "height": int(height),
            "relative_to_window": title,
        },
    }


def capture_widget(window_title: str, widget_name: str) -> dict:
    """Capture a specific Qt widget by object name.

    This requires a Qt-internal capture agent running inside the target
    application (not yet implemented in the OS-level path).  When no such agent
    is reachable, a clear error is raised so the caller can fall back to
    :func:`capture_region` using coordinates.
    """
    raise CaptureError(
        "Qt-internal widget capture is not available. The target Qt application "
        "has no capture agent loaded. Use capture_region with x/y/width/height "
        "(relative to the window via the title argument) instead."
    )