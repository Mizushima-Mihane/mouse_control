"""
鼠标控制工具 — 暴露给 LLM function-calling。
作者: pipi_

基于 pyautogui，提供两套定位体系：

── 手动定位（4 层精度）──
  1. landmark  — 命名地标: "center", "top_left", "bottom_right" 等 9 个点
  2. grid      — 网格定位: 把屏幕切成 rows×cols 格，指定 row/col 编号
  3. pct       — 百分比: x_pct=0.5 即屏幕宽度的 50%
  4. pixel     — 绝对像素: 适合配合截图/OCR 获取精确坐标

── 视觉定位（AI 驱动）──
  5. visual    — Moondream 视觉模型定位: 描述元素，返回精确坐标
     mouse_visual_locate("蓝色的提交按钮") → {x_pct: 0.75, y_pct: 0.3}
     mouse_visual_click("右上角的红色关闭按钮") → 一步到位

推荐的工作流：
  优选:  mouse_visual_click("目标元素描述")
  备选:  mouse_get_screen_size → mouse_move(landmark="center")
         → mouse_move_relative(dx=10, dy=-5) → mouse_click()
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sdk.tool_registry import ToolNotReady, tool

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  截图过滤 — 遮蔽新世界聊天窗口，避免视觉模型看到自己的对话框
# ═══════════════════════════════════════════════════════════════════════

_WINDOW_RECT_CACHE: tuple[int, int, int, int] | None = None
_WINDOW_RECT_TIME: float = 0.0


def _find_shinsekai_window_rect() -> tuple[int, int, int, int] | None:
    """用 Windows API 查找新世界聊天窗口的屏幕矩形。"""
    global _WINDOW_RECT_CACHE, _WINDOW_RECT_TIME
    now = __import__("time").time()
    if _WINDOW_RECT_CACHE is not None and now - _WINDOW_RECT_TIME < 5.0:
        return _WINDOW_RECT_CACHE

    import sys
    if sys.platform != "win32":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        # 枚举顶层窗口，按标题匹配
        found: list[tuple[int, int, int, int]] = []

        def _enum(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            # 匹配 Shinsekai / 新世界 / 桌面助手 等标题
            if title and any(kw in title for kw in ("Shinsekai", "新世界", "桌面助手", "Chat")):
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                if r.right - r.left > 100 and r.bottom - r.top > 100:
                    found.append((r.left, r.top, r.right, r.bottom))
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(_enum), 0)

        if found:
            _WINDOW_RECT_CACHE = found[0]
            _WINDOW_RECT_TIME = now
            return _WINDOW_RECT_CACHE
    except Exception:
        pass
    return None


def _mask_own_window(image):
    """在截图中把新世界聊天窗口涂黑。"""
    rect = _find_shinsekai_window_rect()
    if rect is None:
        return image
    from PIL import ImageDraw
    x1, y1, x2, y2 = rect
    margin = 8  # 稍扩一圈确保遮严
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        [x1 - margin, y1 - margin, x2 + margin, y2 + margin],
        fill=(0, 0, 0),
    )
    return image

MOUSE_TOOL_GROUP = "mouse_control"

# ═══════════════════════════════════════════════════════════════════════
#  Lazy-load pyautogui（避免插件加载时必须已安装）
# ═══════════════════════════════════════════════════════════════════════

_pyautogui = None
_SCREEN_W: int = 0
_SCREEN_H: int = 0
# 用户中断检测：记录上次操作后的光标位置，下次操作前检查用户是否手动移动了鼠标
_last_known_position: tuple[int, int] | None = None
_last_known_time: float = 0.0
_INTERRUPT_THRESHOLD = 75  # 用户移动超过 75px 即视为主动中断
_INTERRUPT_MAX_AGE = 30.0  # 超过 30 秒未操作视为新一轮对话，不检测中断


def _get_pg():
    """延迟导入 pyautogui，首次调用时初始化安全设置并缓存屏幕尺寸。"""
    global _pyautogui, _SCREEN_W, _SCREEN_H
    if _pyautogui is None:
        try:
            import pyautogui as pg
        except ImportError:
            raise ToolNotReady(
                "pyautogui 未安装，请在 plugins/mouse_control 目录运行 install.bat"
            )
        pg.FAILSAFE = False  # 关闭 pyautogui 内置四角锁死，改用自定义中断检测
        pg.PAUSE = 0.03      # 每次操作间短暂停顿，降低被检测/误操作
        _SCREEN_W, _SCREEN_H = pg.size()
        _pyautogui = pg
    return _pyautogui


# ═══════════════════════════════════════════════════════════════════════
#  定位解析 ── 将多种输入模式统一为 (pixel_x, pixel_y)
# ═══════════════════════════════════════════════════════════════════════

_INTERRUPT_RESPONSE = {
    "interrupted": True,
    "message": (
        "检测到用户手动移动了鼠标，char 应停止当前鼠标操作序列。"
        "请告知用户'你动了鼠标，我已停止操作'，不要再继续调用鼠标工具。"
    ),
}


def _check_user_interrupt(pg) -> dict | None:
    """如果用户在上次操作后移动了鼠标，返回中断响应。

    超过 30 秒未操作视为新一轮对话，自动清除中断状态——
    用户回复 char 期间自然会动鼠标（点对话框选项等），不应被当成中断。
    """
    global _last_known_position, _last_known_time
    if _last_known_position is None:
        return None
    # 超过 _INTERRUPT_MAX_AGE 秒 → 新一轮对话，不检测
    if __import__("time").time() - _last_known_time > _INTERRUPT_MAX_AGE:
        _last_known_position = None
        return None
    cx, cy = pg.position()
    lx, ly = _last_known_position
    dist = ((cx - lx) ** 2 + (cy - ly) ** 2) ** 0.5
    if dist > _INTERRUPT_THRESHOLD:
        _last_known_position = None
        return dict(_INTERRUPT_RESPONSE)
    return None


def _track_position(x: int, y: int) -> None:
    """记录操作后的光标位置和时间，供中断检测使用。"""
    global _last_known_position, _last_known_time
    _last_known_position = (x, y)
    _last_known_time = __import__("time").time()

_LANDMARKS: dict[str, tuple[float, float]] = {
    # 9 点命名锚点，值为屏幕占比 (fx, fy)，0.0~1.0
    "center":        (0.5, 0.5),
    "top_left":      (0.0, 0.0),
    "top_center":    (0.5, 0.0),
    "top_right":     (1.0, 0.0),
    "left_center":   (0.0, 0.5),
    "right_center":  (1.0, 0.5),
    "bottom_left":   (0.0, 1.0),
    "bottom_center": (0.5, 1.0),
    "bottom_right":  (1.0, 1.0),
}


def _to_pixels(fx: float, fy: float) -> tuple[int, int]:
    """屏幕占比 (0~1) → 像素坐标。"""
    return int(fx * _SCREEN_W), int(fy * _SCREEN_H)


def _clamp(x: int, y: int) -> tuple[int, int]:
    """将坐标限制在屏幕范围内，并避开四角防止触发热角/系统 UI。"""
    return max(2, min(_SCREEN_W - 3, x)), max(2, min(_SCREEN_H - 3, y))


def _resolve(
    *,
    x_pct: float | None = None,
    y_pct: float | None = None,
    landmark: str | None = None,
    grid_row: int | None = None,
    grid_col: int | None = None,
    grid_rows: int = 3,
    grid_cols: int = 3,
) -> tuple[int, int]:
    """将多种定位输入解析为像素坐标 (x, y)。

    优先级：landmark > grid > pct。
    无像素模式——LLM 猜像素必然不准，强制使用比例定位。
    若全为空，返回当前光标位置。

    Raises:
        ValueError: 未知 landmark 名。
        ToolNotReady: pyautogui 未安装。
    """
    pg = _get_pg()

    # 1. 命名地标
    if landmark is not None:
        key = landmark.strip().lower().replace(" ", "_")
        if key not in _LANDMARKS:
            raise ValueError(
                f"未知地标 '{landmark}'。可用: {', '.join(sorted(_LANDMARKS))}"
            )
        fx, fy = _LANDMARKS[key]
        return _to_pixels(fx, fy)

    # 2. 网格定位（单元格中心点）
    if grid_row is not None and grid_col is not None:
        r, c = int(grid_row), int(grid_col)
        rs, cs = int(grid_rows), int(grid_cols)
        fx = (c - 0.5) / cs
        fy = (r - 0.5) / rs
        return _to_pixels(fx, fy)

    # 3. 百分比定位
    if x_pct is not None and y_pct is not None:
        fx = max(0.0, min(1.0, float(x_pct)))
        fy = max(0.0, min(1.0, float(y_pct)))
        return _to_pixels(fx, fy)

    # fallback: 当前位置
    return pg.position()


# ═══════════════════════════════════════════════════════════════════════
#  Tools
# ═══════════════════════════════════════════════════════════════════════


@tool(
    name="mouse_get_screen_size",
    description=(
        "Get screen resolution in pixels. "
        "ALWAYS call this first so you understand the coordinate space. "
        "Returns width and height as integers."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_get_screen_size() -> dict[str, Any]:
    try:
        _get_pg()
        return {"width": _SCREEN_W, "height": _SCREEN_H}
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


@tool(
    name="mouse_get_position",
    description="Get current mouse cursor position as (x, y) in screen pixels.",
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_get_position() -> dict[str, Any]:
    try:
        pg = _get_pg()
        x, y = pg.position()
        return {"x": x, "y": y}
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


@tool(
    name="mouse_diagnose",
    description=(
        "Diagnose mouse coordinate system accuracy. "
        "Moves cursor to center, then each corner, and reports actual vs expected position. "
        "Use this ONCE to check if pyautogui coordinates match the physical screen. "
        "If discrepancies are reported, all subsequent clicks need correction."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_diagnose() -> dict[str, Any]:
    try:
        pg = _get_pg()
        sw, sh = _SCREEN_W, _SCREEN_H
        results = []
        offsets = []

        test_points = [
            ("center", sw // 2, sh // 2),
            ("top_left", 10, 10),
            ("top_right", sw - 11, 10),
            ("bottom_left", 10, sh - 11),
            ("bottom_right", sw - 11, sh - 11),
        ]
        for label, tx, ty in test_points:
            pg.moveTo(tx, ty, duration=0.1)
            pg.sleep(0.05)
            ax, ay = pg.position()
            dx, dy = abs(ax - tx), abs(ay - ty)
            ok = dx <= 2 and dy <= 2
            results.append({
                "target": label,
                "expected": [tx, ty],
                "actual": [ax, ay],
                "delta": [ax - tx, ay - ty],
                "ok": ok,
            })
            if not ok:
                offsets.append({"label": label, "dx": ax - tx, "dy": ay - ty})

        # 回到中心
        pg.moveTo(sw // 2, sh // 2, duration=0.1)

        return {
            "screen_size": [sw, sh],
            "tests": results,
            "all_ok": len(offsets) == 0,
            "verdict": (
                "坐标系统正常，所有位置偏差 ≤2px。"
                if len(offsets) == 0
                else f"⚠️ {len(offsets)} 个位置有偏差: {offsets} — 可能存在 DPI 缩放或驱动问题"
            ),
        }
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


@tool(
    name="mouse_move",
    description=(
        "Move mouse cursor to a target position. Choose ONE positioning mode:\n"
        "  • landmark: named anchor — 'center', 'top_left', 'top_center', "
        "'top_right', 'left_center', 'right_center', 'bottom_left', "
        "'bottom_center', 'bottom_right'\n"
        "  • grid: divide screen into grid_rows×grid_cols cells, pick cell at "
        "(grid_row, grid_col). 1-based indexing from top-left.\n"
        "    Example: grid_row=2, grid_col=2, grid_rows=3, grid_cols=3 → "
        "center cell of a 3×3 grid\n"
        "  • pct: fraction of screen — x_pct=0.0 is left edge, 1.0 is right edge\n"
        "    Example: x_pct=0.5, y_pct=0.3 → 50% from left, 30% from top\n\n"
        "⚠️ DO NOT guess pixel coordinates — you WILL be wrong. "
        "Always use landmark, grid, or pct (from mouse_visual_locate).\n"
        "Use mouse_move_relative for fine adjustments after coarse positioning.\n"
        "Use duration (0.1–0.5) for smoother, more human-like movement."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_move(
    x_pct: float | None = None,
    y_pct: float | None = None,
    landmark: str | None = None,
    grid_row: int | None = None,
    grid_col: int | None = None,
    grid_rows: int = 3,
    grid_cols: int = 3,
    duration: float = 0.2,
) -> dict[str, Any]:
    try:
        pg = _get_pg()
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        tx, ty = _resolve(
            x_pct=x_pct, y_pct=y_pct,
            landmark=landmark,
            grid_row=grid_row, grid_col=grid_col,
            grid_rows=grid_rows, grid_cols=grid_cols,
        )
        pg.moveTo(tx, ty, duration=duration)
        _track_position(tx, ty)
        return {"x": tx, "y": ty, "status": "moved"}
    except ToolNotReady:
        raise
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


@tool(
    name="mouse_move_relative",
    description=(
        "Move mouse relative to current position. "
        "Positive dx=right, dy=down. "
        "Use after mouse_move for pixel-level fine-tuning. "
        "Example: mouse_move(landmark='center') then mouse_move_relative(dx=50, dy=-20) "
        "moves 50px right and 20px up from center."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_move_relative(
    dx: int,
    dy: int,
    duration: float = 0.1,
) -> dict[str, Any]:
    try:
        pg = _get_pg()
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        pg.moveRel(int(dx), int(dy), duration=duration)
        x, y = pg.position()
        _track_position(x, y)
        return {"x": x, "y": y, "dx": dx, "dy": dy, "status": "moved"}
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


@tool(
    name="mouse_click",
    description=(
        "Click at the current cursor position. "
        "button: 'left', 'right', 'middle'. "
        "clicks: 1 for single-click (default), 2 for double-click. "
        "Use mouse_move + mouse_move_relative to position the cursor first."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_click(button: str = "left", clicks: int = 1) -> dict[str, Any]:
    try:
        pg = _get_pg()
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        b = button or "left"
        n = max(1, min(3, int(clicks)))
        pg.click(button=b, clicks=n)
        return {"button": b, "clicks": n, "status": "clicked"}
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


@tool(
    name="mouse_click_at",
    description=(
        "Move AND click in one step. Supports landmark / grid / pct modes.\n"
        "Shortcut for: mouse_move(...) + mouse_click().\n"
        "Example: mouse_click_at(landmark='center') → click screen center.\n"
        "Example: mouse_click_at(x_pct=0.5, y_pct=0.3, button='right') → right-click at 50%,30%.\n"
        "⚠️ No pixel mode — use pct values from mouse_visual_locate for accuracy."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_click_at(
    x_pct: float | None = None,
    y_pct: float | None = None,
    landmark: str | None = None,
    grid_row: int | None = None,
    grid_col: int | None = None,
    grid_rows: int = 3,
    grid_cols: int = 3,
    button: str = "left",
    clicks: int = 1,
) -> dict[str, Any]:
    try:
        pg = _get_pg()
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        tx, ty = _resolve(
            x_pct=x_pct, y_pct=y_pct,
            landmark=landmark,
            grid_row=grid_row, grid_col=grid_col,
            grid_rows=grid_rows, grid_cols=grid_cols,
        )
        b = button or "left"
        n = max(1, min(3, int(clicks)))
        pg.click(x=tx, y=ty, button=b, clicks=n)
        _track_position(tx, ty)
        return {"x": tx, "y": ty, "button": b, "clicks": n, "status": "clicked"}
    except ToolNotReady:
        raise
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


@tool(
    name="mouse_drag",
    description=(
        "Drag from one position to another (press → move → release). "
        "Both start and end accept landmark / grid / pct modes. "
        "If start is omitted, drags from current cursor position.\n"
        "Example: mouse_drag(start_landmark='top_left', end_landmark='bottom_right') → "
        "select everything from top-left to bottom-right.\n"
        "Example: mouse_drag(end_x_pct=0.5, end_y_pct=0.8) → drag from current position to screen center-bottom."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_drag(
    # ── 起点 ──
    start_x_pct: float | None = None,
    start_y_pct: float | None = None,
    start_landmark: str | None = None,
    # ── 终点 ──
    end_x_pct: float | None = None,
    end_y_pct: float | None = None,
    end_landmark: str | None = None,
    button: str = "left",
    duration: float = 0.3,
) -> dict[str, Any]:
    try:
        pg = _get_pg()
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        b = button or "left"

        # 起点：未指定则用当前位置
        if any(
            v is not None
            for v in [start_x_pct, start_y_pct, start_landmark]
        ):
            sx, sy = _resolve(
                x_pct=start_x_pct, y_pct=start_y_pct,
                landmark=start_landmark,
            )
        else:
            sx, sy = pg.position()

        # 终点
        ex, ey = _resolve(
            x_pct=end_x_pct, y_pct=end_y_pct,
            landmark=end_landmark,
        )

        pg.moveTo(sx, sy)
        pg.dragTo(ex, ey, button=b, duration=duration)
        _track_position(ex, ey)
        return {
            "from": [sx, sy],
            "to": [ex, ey],
            "button": b,
            "status": "dragged",
        }
    except ToolNotReady:
        raise
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


@tool(
    name="mouse_scroll",
    description=(
        "Scroll the mouse wheel. "
        "Positive clicks = scroll UP, negative = scroll DOWN. "
        "Typically 3 clicks ≈ 1 notch on most systems. "
        "Optionally move to a target position before scrolling.\n"
        "Example: mouse_scroll(clicks=-6) → scroll down 2 notches.\n"
        "Example: mouse_scroll(clicks=3, landmark='center') → move to center then scroll up."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_scroll(
    clicks: int = 3,
    x_pct: float | None = None,
    y_pct: float | None = None,
    landmark: str | None = None,
) -> dict[str, Any]:
    try:
        pg = _get_pg()
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        c = int(clicks)

        # 如果指定了位置，先移动
        if any(v is not None for v in [x_pct, y_pct, landmark]):
            tx, ty = _resolve(
                x_pct=x_pct, y_pct=y_pct, landmark=landmark,
            )
            pg.moveTo(tx, ty)
            _track_position(tx, ty)

        pg.scroll(c)
        return {"clicks": c, "status": "scrolled"}
    except ToolNotReady:
        raise
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


@tool(
    name="mouse_press",
    description=(
        "Press and HOLD a mouse button down without releasing. "
        "Use with mouse_release for custom drag-and-drop or multi-step selection. "
        "button: 'left' (default) or 'right'."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_press(button: str = "left") -> dict[str, Any]:
    try:
        pg = _get_pg()
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        b = button or "left"
        pg.mouseDown(button=b)
        return {"button": b, "status": "pressed"}
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


@tool(
    name="mouse_release",
    description=(
        "Release a previously pressed mouse button. "
        "Use after mouse_press to complete a drag or hold action. "
        "button: 'left' (default) or 'right'."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_release(button: str = "left") -> dict[str, Any]:
    try:
        pg = _get_pg()
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        b = button or "left"
        pg.mouseUp(button=b)
        return {"button": b, "status": "released"}
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
#  视觉定位桥梁 — 利用 Moondream 找到屏幕元素坐标，再交给鼠标操作
# ═══════════════════════════════════════════════════════════════════════

_VISUAL_IMPORT_ERROR: str | None = None


def _show_busy(text: str = "鼠标控制: 正在识屏…") -> None:
    """在聊天窗底部显示忙碌提示条。"""
    try:
        from ui.chat_ui.context import try_get_chat_ui_context
        ctx = try_get_chat_ui_context()
        if ctx is not None:
            ctx.set_busy_bar(text, 0.0)
    except Exception:
        pass


def _hide_busy() -> None:
    """隐藏忙碌提示条。"""
    try:
        from ui.chat_ui.context import try_get_chat_ui_context
        ctx = try_get_chat_ui_context()
        if ctx is not None:
            ctx.hide_busy_bar()
    except Exception:
        pass


def _try_import_moondream_modules() -> tuple | None:
    """尝试导入 moondream 相关模块；失败时缓存错误信息。

    截图使用 mss（物理分辨率），比 pyautogui.screenshot()（逻辑分辨率）
    在高 DPI 下清晰 4 倍，Moondream 定位精度随之提升。
    坐标转换用分数（0~1），不受分辨率差异影响。
    """
    global _VISUAL_IMPORT_ERROR
    if _VISUAL_IMPORT_ERROR is not None:
        return None
    try:
        from plugins.moondream_vision.capture_infer import grab_screen_png  # noqa: F811
        from plugins.moondream_vision.config_model import load_config  # noqa: F811
        from plugins.moondream_vision.local_infer import (  # noqa: F811
            get_model,
            is_tool_ready,
            start_preload_model,
            loading_status_message,
        )
        from plugins.moondream_vision import runtime  # noqa: F811

        return (
            grab_screen_png,
            load_config,
            get_model,
            is_tool_ready,
            start_preload_model,
            loading_status_message,
            runtime,
        )
    except ImportError as e:
        _VISUAL_IMPORT_ERROR = str(e)
        return None


@tool(
    name="mouse_visual_locate",
    description=(
        "Find a UI element on screen using the vision model. "
        "IMPORTANT: Use simple English category names like 'button', 'close icon', "
        "'text input', 'menu', 'checkbox'. Do NOT use complex descriptions — "
        "the model only understands simple object categories.\n"
        "GOOD: 'button', 'close icon', 'text input', 'menu'\n"
        "BAD: 'the blue Submit button in the bottom right corner' (too complex)\n\n"
        "Returns x_pct/y_pct that can be used with mouse_click_at(x_pct=...).\n"
        "If there are multiple matches, use the first one and refine with mouse_move_relative.\n"
        "NOTE: requires Moondream vision plugin. First use triggers model download (2-10 min)."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_visual_locate(description: str) -> dict[str, Any]:
    desc = (description or "").strip()
    if not desc:
        return {"error": "description 不能为空 — 请描述你要找的屏幕元素。"}

    _show_busy(f"鼠标控制: 正在寻找「{desc[:30]}」…")
    try:
        return _mouse_visual_locate_impl(desc)
    finally:
        _hide_busy()


def _mouse_visual_locate_impl(desc: str) -> dict[str, Any]:
    mods = _try_import_moondream_modules()
    if mods is None:
        return {
            "error": (
                "视觉定位需要 Moondream 识屏插件。"
                "请在插件管理器中启用 moondream_vision。"
                + (f" 导入失败: {_VISUAL_IMPORT_ERROR}" if _VISUAL_IMPORT_ERROR else "")
            )
        }

    (
        grab_screen_png,
        load_config,
        get_model,
        is_tool_ready,
        start_preload_model,
        loading_status_message,
        runtime,
    ) = mods

    try:
        cfg_path = runtime.plugin_config_path()
    except RuntimeError:
        return {"error": "Moondream 插件尚未完成初始化，请稍后再试。"}

    cfg = load_config(cfg_path)

    if not is_tool_ready():
        start_preload_model(cfg)
        raise ToolNotReady(loading_status_message())

    try:
        import io
        from PIL import Image
    except ImportError as e:
        return {"error": f"PIL/Pillow 未安装: {e}"}

    try:
        # 使用 mss 截图（物理分辨率），比 pyautogui.screenshot() 清晰。
        # 在高 DPI 下 mss 提供 4 倍像素 → Moondream 定位精度大幅提升。
        # 坐标使用分数（0~1），与 pyautogui 操控坐标系兼容。
        png = grab_screen_png(cfg.monitor_index)
        image = Image.open(io.BytesIO(png)).convert("RGB")
        image = _mask_own_window(image)
        img_w, img_h = image.size
        model = get_model(cfg)
    except Exception as e:
        logger.exception("视觉定位: 截图或模型加载失败")
        return {"error": f"截图或模型加载失败: {e}"}

    # ── 调用 Moondream point API ──────────────────────────────
    # point() 只认简单类别名（"button"、"close icon"），不认复杂描述。
    # 从用户描述中提取核心名词作为 point 查询词。
    _simple_terms = ["button", "icon", "text", "menu", "link", "input",
                     "checkbox", "close", "submit", "search", "folder", "file"]
    point_query = desc
    for _term in _simple_terms:
        if _term in desc.lower():
            point_query = _term
            break
    try:
        import torch

        with torch.inference_mode():
            result = model.point(image, point_query)
        points = result.get("points", []) if isinstance(result, dict) else []
    except AttributeError:
        return {
            "error": (
                "当前 Moondream 模型版本不支持 point/detect API。"
                "请升级到 moondream2 最新版: pip install --upgrade moondream"
            )
        }
    except Exception as e:
        logger.exception("视觉定位: model.point() 失败")
        return {"error": f"视觉定位失败: {e}"}

    if not points:
        return {
            "error": (
                f"未在屏幕上找到 '{point_query}' 类元素（原描述: '{desc}'）。"
                "请尝试用更简单的英文类别名如 'button'、'icon'、'text'。"
            )
        }

    pt = points[0]
    fx = float(pt.get("x", 0.5))
    fy = float(pt.get("y", 0.5))
    fx = max(0.0, min(1.0, fx))
    fy = max(0.0, min(1.0, fy))

    # 用 pyautogui 的屏幕尺寸（逻辑像素）而非截图尺寸（物理像素）计算像素坐标。
    # 在 Windows DPI 缩放环境下两者相差 ×2 或更多，用错坐标系统会导致点击严重偏移。
    pg = _get_pg()
    return {
        "description": desc,
        "x_pct": round(fx, 5),
        "y_pct": round(fy, 5),
        "x_pixel": int(fx * _SCREEN_W),
        "y_pixel": int(fy * _SCREEN_H),
        "screen_size": [_SCREEN_W, _SCREEN_H],
        "image_size": [img_w, img_h],
        "note": (
            "IMPORTANT: use x_pct/y_pct with mouse_click_at(x_pct=..., y_pct=...) "
            "for maximum accuracy. The x_pixel/y_pixel values are in pyautogui "
            "coordinates; do NOT use raw image pixel values for clicking."
        ),
    }


def _omniparser_search(description: str) -> dict[str, Any] | None:
    """用 OmniParser 搜索屏幕元素，找到匹配则返回坐标，失败返回 None。"""
    try:
        pg = _get_pg()
        screenshot = pg.screenshot()
        result = _call_omniparser(screenshot)
    except Exception:
        return None
    if "error" in result or not result.get("elements"):
        return None
    query = description.strip().lower()
    matches = []
    for e in result["elements"]:
        text = e.get("text", "").lower()
        etype = e.get("type", "").lower()
        if query in text or query in etype:
            matches.append(e)
    if not matches:
        return None
    best = matches[0]
    return {
        "method": "omniparser",
        "description": description,
        "x_pct": best["x_pct"],
        "y_pct": best["y_pct"],
        "x_pixel": int(best["x_pct"] * _SCREEN_W),
        "y_pixel": int(best["y_pct"] * _SCREEN_H),
        "screen_size": [_SCREEN_W, _SCREEN_H],
        "matched_element": best,
    }


def _visual_locate_best(description: str) -> dict[str, Any]:
    """视觉定位最佳策略：OmniParser > Moondream detect > Moondream point > OCR"""
    # 1. 先试 OmniParser（UI 专训，最准）
    omni = _omniparser_search(description)
    if omni is not None:
        return omni

    # 2. 再试 Moondream detect
    detect_result = _visual_detect_impl(description)
    if "error" not in detect_result and detect_result.get("objects"):
        best = detect_result["objects"][0]
        fx = (best["x1"] + best["x2"]) / 2.0
        fy = (best["y1"] + best["y2"]) / 2.0
        return {
            "method": "detect",
            "description": description,
            "x_pct": round(fx, 5),
            "y_pct": round(fy, 5),
            "x_pixel": int(fx * _SCREEN_W),
            "y_pixel": int(fy * _SCREEN_H),
            "screen_size": [_SCREEN_W, _SCREEN_H],
            "bounding_box": best,
        }

    # 3. fallback 到 point
    return mouse_visual_locate(description)


def _visual_detect_impl(description: str) -> dict[str, Any]:
    """调用 Moondream detect API，返回包围盒列表。"""
    desc = (description or "").strip()
    if not desc:
        return {"error": "description 不能为空"}

    mods = _try_import_moondream_modules()
    if mods is None:
        return {
            "error": (
                "视觉定位需要 Moondream 识屏插件。"
                + (f" 导入失败: {_VISUAL_IMPORT_ERROR}" if _VISUAL_IMPORT_ERROR else "")
            )
        }
    (
        grab_screen_png,
        load_config,
        get_model,
        is_tool_ready,
        start_preload_model,
        loading_status_message,
        runtime,
    ) = mods

    try:
        cfg_path = runtime.plugin_config_path()
    except RuntimeError:
        return {"error": "Moondream 插件尚未完成初始化，请稍后再试。"}

    cfg = load_config(cfg_path)

    if not is_tool_ready():
        start_preload_model(cfg)
        raise ToolNotReady(loading_status_message())

    try:
        import io
        from PIL import Image
    except ImportError as e:
        return {"error": f"PIL/Pillow 未安装: {e}"}

    try:
        # mss 高清截图 = 更好的 Moondream 定位精度
        png = grab_screen_png(cfg.monitor_index)
        image = Image.open(io.BytesIO(png)).convert("RGB")
        image = _mask_own_window(image)
        model = get_model(cfg)
    except Exception as e:
        logger.exception("视觉 detect: 截图或模型加载失败")
        return {"error": f"截图或模型加载失败: {e}"}

    try:
        import torch

        with torch.inference_mode():
            result = model.detect(image, desc)
        objects = result.get("objects", []) if isinstance(result, dict) else []
    except AttributeError:
        return {
            "error": (
                "当前 Moondream 模型版本不支持 detect API。"
                "请升级到 moondream2 最新版: pip install --upgrade moondream"
            )
        }
    except Exception as e:
        logger.exception("视觉 detect: model.detect() 失败")
        return {"error": f"视觉 detect 失败: {e}"}

    if not objects:
        return {"error": f"未在屏幕上检测到与 '{desc}' 匹配的元素。"}

    # 把归一化坐标转为人可读格式
    cleaned = []
    for obj in objects[:10]:
        cleaned.append({
            "label": obj.get("label", ""),
            "x1": round(float(obj.get("x1", 0)), 5),
            "y1": round(float(obj.get("y1", 0)), 5),
            "x2": round(float(obj.get("x2", 0)), 5),
            "y2": round(float(obj.get("y2", 0)), 5),
        })
    return {"description": desc, "objects": cleaned, "count": len(cleaned)}


@tool(
    name="mouse_visual_detect",
    description=(
        "Detect UI elements on screen and return bounding boxes. "
        "IMPORTANT: Use simple English category names — 'button', 'icon', 'text'.\n"
        "Example: mouse_visual_detect('button') or mouse_visual_detect('close icon').\n"
        "NOTE: requires Moondream vision plugin."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_visual_detect(description: str) -> dict[str, Any]:
    _show_busy(f"鼠标控制: 正在检测「{description[:30]}」…")
    try:
        return _visual_detect_impl(description)
    finally:
        _hide_busy()


@tool(
    name="mouse_visual_click",
    description=(
        "Find a UI element on screen using vision and CLICK it. "
        "IMPORTANT: Use simple English category names — 'button', 'close icon', 'menu'. "
        "Do NOT use complex descriptions — the model only understands categories.\n"
        "Example: mouse_visual_click('button') for a generic button.\n"
        "Example: mouse_visual_click('close icon') for a close/X button.\n"
        "For clicking specific TEXT, use mouse_click_text() instead — it's far more accurate.\n"
        "NOTE: requires Moondream vision plugin."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_visual_click(
    description: str,
    button: str = "left",
    clicks: int = 1,
) -> dict[str, Any]:
    """视觉定位 + 点击：detect 优先，包围盒中心更精准。"""
    _show_busy(f"鼠标控制: 正在寻找「{description[:30]}」…")
    try:
        return _mouse_visual_click_impl(description, button, clicks)
    finally:
        _hide_busy()


def _mouse_visual_click_impl(description: str, button: str, clicks: int) -> dict[str, Any]:
    locate_result = _visual_locate_best(description)
    if "error" in locate_result:
        return locate_result

    fx = locate_result["x_pct"]
    fy = locate_result["y_pct"]

    try:
        pg = _get_pg()
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        tx, ty = _to_pixels(fx, fy)
        tx, ty = _clamp(tx, ty)
        b = button or "left"
        n = max(1, min(3, int(clicks)))
        pg.click(x=tx, y=ty, button=b, clicks=n)
        _track_position(tx, ty)
        return {
            "description": description,
            "method": locate_result.get("method", "point"),
            "x_pct": fx,
            "y_pct": fy,
            "x_pixel": tx,
            "y_pixel": ty,
            "button": b,
            "clicks": n,
            "status": "clicked",
        }
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
#  OCR 文字定位 — 像素级精确，无需视觉模型猜
# ═══════════════════════════════════════════════════════════════════════

_OCR_IMPORT_ERROR: str | None = None


def _ocr_find_text(lookup: str) -> list[dict]:
    """OCR 全屏，返回匹配文字的包围盒中心坐标。"""
    global _OCR_IMPORT_ERROR
    if _OCR_IMPORT_ERROR is not None:
        return []

    try:
        from plugins.moondream_vision.chinese_ocr import ocr_image_with_boxes
    except ImportError as e:
        _OCR_IMPORT_ERROR = str(e)
        return []

    pg = _get_pg()
    image = pg.screenshot().convert("RGB")
    image = _mask_own_window(image)
    img_w, img_h = image.size
    items = ocr_image_with_boxes(image)

    query = lookup.strip().lower()
    matches: list[dict] = []
    for item in items:
        if query in item["text"].lower():
            box = item["box"]
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = (min(xs) + max(xs)) / 2.0
            cy = (min(ys) + max(ys)) / 2.0
            matches.append({
                "text": item["text"],
                "x_pct": round(cx / img_w, 5),
                "y_pct": round(cy / img_h, 5),
                "x_pixel": int(cx),
                "y_pixel": int(cy),
            })
    return matches


@tool(
    name="mouse_find_text",
    description=(
        "Find text on screen using OCR and return its exact coordinates. "
        "MUCH more accurate than visual_locate for any text-based UI element. "
        "Matches partial text — 'Submit' finds 'Submit', '提交' etc.\n"
        "Returns x_pct/y_pct ready for mouse_click_at(...).\n"
        "Example: mouse_find_text('登录') → finds the login button.\n"
        "NOTE: requires rapidocr-onnxruntime (pip install rapidocr-onnxruntime)."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_find_text(lookup: str) -> dict[str, Any]:
    if not (lookup or "").strip():
        return {"error": "lookup 不能为空 — 请提供要查找的文字。"}
    _show_busy(f"鼠标控制: 正在搜索文字「{lookup[:20]}」…")
    try:
        return _mouse_find_text_impl(lookup)
    finally:
        _hide_busy()


def _mouse_find_text_impl(lookup: str) -> dict[str, Any]:
    matches = _ocr_find_text(lookup)
    if not matches:
        if _OCR_IMPORT_ERROR:
            return {"error": f"OCR 引擎不可用: {_OCR_IMPORT_ERROR}"}
        return {"error": f"未在屏幕上找到包含 '{lookup}' 的文字。"}
    return {"lookup": lookup, "count": len(matches), "matches": matches[:20], "best": matches[0]}


@tool(
    name="mouse_click_text",
    description=(
        "Find text on screen using OCR and CLICK it — the MOST accurate way to click "
        "buttons, menus, links, and any text element.\n"
        "Example: mouse_click_text('登录') → clicks the login button.\n"
        "Example: mouse_click_text('关闭') → clicks the close button.\n"
        "Use match_index to pick among multiple matches (0=first, 1=second…).\n"
        "NOTE: for non-text icons/images, use mouse_visual_click instead."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_click_text(
    lookup: str,
    match_index: int = 0,
    button: str = "left",
    clicks: int = 1,
) -> dict[str, Any]:
    if not (lookup or "").strip():
        return {"error": "lookup 不能为空。"}
    _show_busy(f"鼠标控制: 正在搜索文字「{lookup[:20]}」…")
    try:
        matches = _ocr_find_text(lookup)
        if not matches:
            if _OCR_IMPORT_ERROR:
                return {"error": f"OCR 引擎不可用: {_OCR_IMPORT_ERROR}"}
            return {"error": f"未在屏幕上找到包含 '{lookup}' 的文字。"}
        idx = max(0, min(len(matches) - 1, int(match_index)))
        target = matches[idx]
        try:
            pg = _get_pg()
            intr = _check_user_interrupt(pg)
            if intr:
                return intr
            fx, fy = target["x_pct"], target["y_pct"]
            tx, ty = _to_pixels(fx, fy)
            tx, ty = _clamp(tx, ty)
            b, n = button or "left", max(1, min(3, int(clicks)))
            pg.click(x=tx, y=ty, button=b, clicks=n)
            _track_position(tx, ty)
            return {
                "lookup": lookup, "matched_text": target["text"],
                "match_index": idx, "total_matches": len(matches),
                "x_pct": fx, "y_pct": fy,
                "x_pixel": tx, "y_pixel": ty,
                "button": b, "clicks": n, "status": "clicked",
            }
        except ToolNotReady:
            raise
        except Exception as e:
            return {"error": str(e)}
    finally:
        _hide_busy()


# ═══════════════════════════════════════════════════════════════════════
#  网格标注定位 — 截图画网格 → query() 识别目标格子 → 点击
# ═══════════════════════════════════════════════════════════════════════

def _annotate_grid(image, rows: int = 4, cols: int = 4):
    """在 PIL Image 上绘制带行列编号的网格。"""
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(image)
    w, h = image.size
    cell_w, cell_h = w / cols, h / rows
    for i in range(1, cols):
        draw.line([(int(i * cell_w), 0), (int(i * cell_w), h)], fill=(255, 0, 0, 128), width=2)
    for i in range(1, rows):
        draw.line([(0, int(i * cell_h)), (w, int(i * cell_h))], fill=(255, 0, 0, 128), width=2)
    try:
        font = ImageFont.truetype("arial.ttf", max(14, min(w, h) // 40))
    except Exception:
        font = ImageFont.load_default()
    for r in range(rows):
        for c in range(cols):
            draw.text(
                (int((c + 0.4) * cell_w), int((r + 0.35) * cell_h)),
                f"({r},{c})", fill=(255, 255, 0), font=font,
            )
    return image


def _grid_answer_to_pct(answer: str, rows: int, cols: int) -> tuple[float, float] | None:
    """从 Moondream 回答中解析行列号，转为屏幕百分比坐标。"""
    # "(r,c)" / "(r, c)" / "row r col c" / "第r行第c列"
    for pat in [
        r"\(\s*(\d+)\s*[,，]\s*(\d+)\s*\)",
        r"row\s*(\d+).*?col\s*(\d+)",
        r"第\s*(\d+)\s*行.*?第\s*(\d+)\s*列",
    ]:
        m = re.search(pat, answer, re.IGNORECASE)
        if m:
            r, c = int(m.group(1)), int(m.group(2))
            return (c + 0.5) / cols, (r + 0.5) / rows
    return None


@tool(
    name="mouse_visual_grid_click",
    description=(
        "Divide screen into a labeled grid, ask vision model which cell contains "
        "the target, then click that cell's center. "
        "MUCH more reliable than point/detect — the model only answers a simple "
        "'which cell' question instead of guessing exact pixel coordinates.\n"
        "Example: mouse_visual_grid_click('the submit button', rows=4, cols=4)\n"
        "Use higher rows/cols for more precision, lower for better identification."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_visual_grid_click(
    description: str,
    rows: int = 4,
    cols: int = 4,
    button: str = "left",
) -> dict[str, Any]:
    desc = (description or "").strip()
    if not desc:
        return {"error": "description 不能为空。"}
    _show_busy(f"鼠标控制: 网格定位「{desc[:30]}」…")
    try:
        return _mouse_visual_grid_click_impl(desc, rows, cols, button)
    finally:
        _hide_busy()


def _mouse_visual_grid_click_impl(
    desc: str, rows: int, cols: int, button: str
) -> dict[str, Any]:
    mods = _try_import_moondream_modules()
    if mods is None:
        return {"error": "视觉定位需要 Moondream 识屏插件。"}
    grab_screen_png, load_config, get_model, is_tool_ready, \
        start_preload_model, loading_status_message, runtime = mods
    try:
        cfg_path = runtime.plugin_config_path()
    except RuntimeError:
        return {"error": "Moondream 插件尚未完成初始化。"}
    cfg = load_config(cfg_path)
    if not is_tool_ready():
        start_preload_model(cfg)
        raise ToolNotReady(loading_status_message())
    try:
        import io
        from PIL import Image
    except ImportError as e:
        return {"error": f"PIL 未安装: {e}"}
    try:
        png = grab_screen_png(cfg.monitor_index)
        image = Image.open(io.BytesIO(png)).convert("RGB")
        image = _mask_own_window(image)
        r, c = max(2, min(8, int(rows))), max(2, min(8, int(cols)))
        annotated = _annotate_grid(image.copy(), rows=r, cols=c)
        model = get_model(cfg)
    except Exception as e:
        return {"error": f"截图失败: {e}"}
    prompt = (
        f"This image has a {r}x{c} grid overlay labeled (row,col). "
        f"Which grid cell contains '{desc}'? "
        f"Answer ONLY with the cell coordinate like '(2,3)'. No explanation."
    )
    try:
        import torch
        with torch.inference_mode():
            raw = model.query(annotated, prompt)
        answer = str(raw.get("answer", "") if isinstance(raw, dict) else raw)
    except Exception as e:
        return {"error": f"Moondream query 失败: {e}"}
    result = _grid_answer_to_pct(answer, r, c)
    if result is None:
        return {"error": f"无法从回答解析网格坐标。回答: '{answer[:200]}'。请重试。"}
    fx, fy = result
    try:
        pg = _get_pg()
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        tx, ty = _to_pixels(fx, fy)
        tx, ty = _clamp(tx, ty)
        b = button or "left"
        pg.click(x=tx, y=ty, button=b)
        _track_position(tx, ty)
        return {
            "description": desc, "grid": f"{r}×{c}",
            "raw_answer": answer[:200],
            "x_pct": round(fx, 5), "y_pct": round(fy, 5),
            "x_pixel": tx, "y_pixel": ty,
            "button": b, "status": "clicked",
        }
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
#  OmniParser 桥梁 — 调本地 Gradio API 获取像素级 UI 元素坐标
#  启动: cd OmniParser && F:\minicond\envs\omni\Scripts\python.exe gradio_demo.py
#  配置: 插件设置 → OmniParser 识屏
# ═══════════════════════════════════════════════════════════════════════


def _get_omniparser_config() -> tuple[str, float, float]:
    """读取 OmniParser 配置。"""
    from pathlib import Path
    config_path = Path(__file__).resolve().parent / "omniparser_config.json"
    if config_path.exists():
        try:
            import json
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return (
                cfg.get("server_url", "http://127.0.0.1:7861"),
                float(cfg.get("box_threshold", 0.03)),
                float(cfg.get("iou_threshold", 0.1)),
            )
        except Exception:
            pass
    return ("http://127.0.0.1:7861", 0.03, 0.1)


def _call_omniparser(screenshot) -> dict[str, Any]:
    """调用 OmniParser 独立 HTTP 服务，返回结构化 UI 元素列表。

    服务启动方式:
      F:\\minicond\\envs\\omni\\Scripts\\python.exe omni_server.py
    端口: 7862
    """
    url, box_thr, iou_thr = _get_omniparser_config()
    # 将 Gradio 端口 (7861) 替换为独立服务端口 (7862)
    api_url = url.rstrip("/").replace(":7861", ":7862") + "/process"

    import io
    screenshot = _mask_own_window(screenshot)
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    try:
        import urllib.request
        req = urllib.request.Request(api_url, data=png_bytes, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read().decode())
        if data.get("ok"):
            elements = data.get("elements", [])
            # 标准化字段名
            for e in elements:
                if "x_pct" not in e and "bbox" in e:
                    bbox = e["bbox"]
                    e["x_pct"] = round((bbox[0] + bbox[2]) / 2, 5)
                    e["y_pct"] = round((bbox[1] + bbox[3]) / 2, 5)
            return {"count": len(elements), "elements": elements[:50]}
        else:
            return {"error": data.get("error", "Unknown OmniParser error")}
    except Exception as e:
        return {
            "error": (
                f"OmniParser 服务不可用 ({api_url})。\n"
                f"请先启动: F:\\minicond\\envs\\omni\\Scripts\\python.exe "
                f"F:\\Alter Ego\\Shinsekai 1.6.2\\Shinsekai\\OmniParser\\omni_server.py\n"
                f"错误: {e}"
            )
        }


@tool(
    name="mouse_omniparser_locate",
    description=(
        "Use OmniParser (Microsoft UI parsing model) to detect ALL UI elements on screen "
        "with pixel-precise bounding boxes. "
        "Returns {type, bbox, text} for every button, icon, text field.\n"
        "MUCH more accurate than Moondream — OmniParser is trained specifically for UI.\n"
        "NOTE: requires OmniParser Gradio server running locally on port 7861."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_omniparser_locate() -> dict[str, Any]:
    _show_busy("鼠标控制: OmniParser 正在解析屏幕…")
    try:
        pg = _get_pg()
        screenshot = pg.screenshot()
        result = _call_omniparser(screenshot)
    finally:
        _hide_busy()
    return result


@tool(
    name="mouse_omniparser_click",
    description=(
        "Use OmniParser to find a UI element by text or type and CLICK it. "
        "Matches partial text — 'login' finds 'Login button'.\n"
        "Example: mouse_omniparser_click('login') — clicks element containing 'login'.\n"
        "Example: mouse_omniparser_click('button', match_index=1) — clicks 2nd button.\n"
        "NOTE: requires OmniParser Gradio server running locally on port 7861."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_omniparser_click(
    lookup: str,
    match_index: int = 0,
    button: str = "left",
) -> dict[str, Any]:
    if not (lookup or "").strip():
        return {"error": "lookup 不能为空。"}
    locate_result = mouse_omniparser_locate()
    if "error" in locate_result:
        return locate_result
    elements = locate_result.get("elements", [])
    query = lookup.strip().lower()
    matches = [e for e in elements if query in e.get("text", "").lower() or query in e.get("type", "").lower()]
    if not matches:
        return {"error": f"OmniParser 未找到 '{lookup}'。共 {len(elements)} 个元素。"}
    idx = max(0, min(len(matches) - 1, int(match_index)))
    target = matches[idx]
    try:
        pg = _get_pg()
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        fx, fy = target["x_pct"], target["y_pct"]
        tx, ty = _to_pixels(fx, fy)
        tx, ty = _clamp(tx, ty)
        b = button or "left"
        pg.click(x=tx, y=ty, button=b)
        _track_position(tx, ty)
        return {
            "lookup": lookup, "matched": target["text"] or target["type"],
            "match_index": idx, "total_matches": len(matches),
            "x_pct": fx, "y_pct": fy,
            "x_pixel": tx, "y_pixel": ty,
            "button": b, "status": "clicked",
        }
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
#  智能点击 — OmniParser 精确坐标 + Moondream 语义理解
# ═══════════════════════════════════════════════════════════════════════

def _win32_drag(x1: int, y1: int, x2: int, y2: int, button: str = "left", duration: float = 0.5):
    """用 Windows SendInput 实现真正的长按拖拽。

    游戏等）中不被识别为持续按住。SendInput 是更底层的 API。
    关键：移动阶段用 RELATIVE 坐标（非绝对），应用才能正确识别为拖拽轨迹。
    """
    import ctypes
    from ctypes import wintypes, byref, sizeof, Structure, Union

    INPUT_MOUSE = 0
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010

    if button == "right":
        down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    else:
        down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP

    class _MOUSEINPUT(Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _INPUT_UNION(Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    class _INPUT(Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_UNION)]

    user32 = ctypes.windll.user32

    def _abs_coord(v: int, axis_size: int) -> int:
        return int(v * 65536 / axis_size) if axis_size > 0 else 0

    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)

    steps = max(10, int(duration * 40))
    total_events = 1 + steps + 1  # down + moves + up
    inputs = (_INPUT * total_events)()
    idx = 0

    # 0. 先移到起点（绝对坐标）
    inputs[idx].type = INPUT_MOUSE
    inputs[idx].union.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    inputs[idx].union.mi.dx = _abs_coord(x1, sw)
    inputs[idx].union.mi.dy = _abs_coord(y1, sh)
    idx += 1

    # 1. 按下
    inputs[idx].type = INPUT_MOUSE
    inputs[idx].union.mi.dwFlags = down
    idx += 1

    # 2. 移动（相对坐标——关键！应用靠相对位移识别拖拽）
    total_dx = x2 - x1
    total_dy = y2 - y1
    for i in range(1, steps + 1):
        # 每一步发一小段相对位移
        step_dx = int(total_dx / steps)
        step_dy = int(total_dy / steps)
        # 最后一步补偿取整误差
        if i == steps:
            step_dx = total_dx - int(total_dx / steps) * (steps - 1)
            step_dy = total_dy - int(total_dy / steps) * (steps - 1)
        inputs[idx].type = INPUT_MOUSE
        inputs[idx].union.mi.dwFlags = MOUSEEVENTF_MOVE
        inputs[idx].union.mi.dx = step_dx
        inputs[idx].union.mi.dy = step_dy
        idx += 1

    # 3. 松开
    inputs[idx].type = INPUT_MOUSE
    inputs[idx].union.mi.dwFlags = up
    idx += 1

    user32.SendInput(idx, byref(inputs), sizeof(_INPUT))
    import time as _time
    _time.sleep(duration * 0.15)


def _moondream_point_coords(desc: str) -> tuple[float, float] | None:
    """Moondream 定位坐标。

    策略：
    - 标题栏/窗口类 → point()（指向特定区域特征，比 detect 框整个窗口更准）
    - 按钮/图标类 → detect() 包围盒中心（比单点更稳）
    """
    try:
        from plugins.moondream_vision.capture_infer import grab_screen_png
        from plugins.moondream_vision.config_model import load_config
        from plugins.moondream_vision.local_infer import (
            get_model, is_tool_ready, start_preload_model, loading_status_message,
        )
        from plugins.moondream_vision import runtime
        import io as _io
        from PIL import Image as _Image

        cfg_path = runtime.plugin_config_path()
        cfg = load_config(cfg_path)
        if not is_tool_ready():
            start_preload_model(cfg)
            return None
        png = grab_screen_png(cfg.monitor_index)
        image = _Image.open(_io.BytesIO(png)).convert("RGB")
        image = _mask_own_window(image)
        model = get_model(cfg)
        import torch as _torch

        is_window = any(kw in desc.lower() for kw in (
            "title", "标题", "window", "窗口", "bar", "栏", "drag", "拖", "tab", "标签",
        ))

        if is_window:
            # 窗口标题栏：detect 框出窗口 → 取顶部中央（标题栏区域）
            with _torch.inference_mode():
                det_result = model.detect(image, "window")
            objects = det_result.get("objects", []) if isinstance(det_result, dict) else []
            if objects:
                obj = objects[0]
                fx = (float(obj.get("x_min", 0)) + float(obj.get("x_max", 0))) / 2.0
                y_top = float(obj.get("y_min", 0))
                y_bottom = float(obj.get("y_max", 0))
                # 标题栏在窗口顶部 8% 区域内
                fy = y_top + (y_bottom - y_top) * 0.04
                return (fx, fy)

        # 按钮/图标类：detect 包围盒中心
        with _torch.inference_mode():
            det_result = model.detect(image, desc)
        objects = det_result.get("objects", []) if isinstance(det_result, dict) else []
        if objects:
            obj = objects[0]
            fx = (float(obj.get("x_min", 0)) + float(obj.get("x_max", 0))) / 2.0
            fy = (float(obj.get("y_min", 0)) + float(obj.get("y_max", 0))) / 2.0
            return (fx, fy)

        # 都不行 → point()
        with _torch.inference_mode():
            result = model.point(image, desc)
        points = result.get("points", []) if isinstance(result, dict) else []
        if points:
            pt = points[0]
            return (float(pt.get("x", 0.5)), float(pt.get("y", 0.5)))
    except Exception:
        pass
    return None


def _moondream_help_identify(desc: str, elements: list) -> str | None:
    """用 Moondream 辅助识别目标元素文本/类型标签。"""
    try:
        from plugins.moondream_vision.capture_infer import grab_screen_png
        from plugins.moondream_vision.config_model import load_config
        from plugins.moondream_vision.local_infer import (
            get_model, is_tool_ready, start_preload_model, loading_status_message,
        )
        from plugins.moondream_vision import runtime
        import io as _io
        from PIL import Image as _Image

        cfg_path = runtime.plugin_config_path()
        cfg = load_config(cfg_path)
        if not is_tool_ready():
            start_preload_model(cfg)
            return None
        png = grab_screen_png(cfg.monitor_index)
        image = _Image.open(_io.BytesIO(png)).convert("RGB")
        image = _mask_own_window(image)
        model = get_model(cfg)
        el_summary = ", ".join(
            f"[{e.get('type','?')}] \"{e.get('text','')[:30]}\""
            for e in elements[:25]
        )
        import torch as _torch
        with _torch.inference_mode():
            raw = model.query(image, (
                f"I need to click: '{desc}'. "
                f"Visible UI elements: {el_summary}. "
                f"Which element should I click? Answer with ONLY the element's "
                f"text label or type name. One short phrase."
            ))
        answer = str(raw.get("answer", "") if isinstance(raw, dict) else raw)[:80]
        import re as _re
        short = _re.sub(r'[^a-zA-Z0-9一-鿿\s]', '', answer).strip()
        return short if short else None
    except Exception:
        return None


@tool(
    name="mouse_smart_click",
    description=(
        "THE most accurate way to click anything. OmniParser + Moondream combined.\n"
        "1. OmniParser scans UI elements with pixel-exact coordinates\n"
        "2. Text match first (fast, accurate)\n"
        "3. If no match, Moondream visually identifies the target\n"
        "4. Clicks best match\n"
        "Example: mouse_smart_click('the submit button')\n"
        "Example: mouse_smart_click('close the error dialog')"
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_smart_click(
    description: str,
    button: str = "left",
) -> dict[str, Any]:
    desc = (description or "").strip()
    if not desc:
        return {"error": "description 不能为空。"}

    pg = _get_pg()

    # ── 判断是否窗口类操作 ──────────────────────────────────
    # 拖窗口/标题栏/关闭按钮等动态元素，Moondream 视觉定位比 OmniParser 准
    _WINDOW_KW = [
        "window", "窗口", "title", "标题", "drag", "拖", "close", "关闭",
        "minimize", "最小化", "maximize", "最大化", "bar", "栏", "border", "边框",
        "tab", "标签页", "chrome", "titlebar",
    ]
    is_window_query = any(kw in desc.lower() for kw in _WINDOW_KW)

    # ── OmniParser 扫描 ────────────────────────────────────
    omni = _call_omniparser(pg.screenshot())
    elements = omni.get("elements", []) if isinstance(omni, dict) else []
    omni_ok = "error" not in omni and len(elements) > 0

    # 文字匹配
    q = desc.lower()
    text_ok = [e for e in elements if q in (e.get("text", "") or "").lower()]
    type_ok = [e for e in elements if q in (e.get("type", "") or "").lower()]

    best = None
    method = ""
    moondream_coords = None

    # ── 窗口类: Moondream point() 优先 ──────────────────────
    if is_window_query:
        moondream_coords = _moondream_point_coords(desc)
        if moondream_coords is not None:
            best = {"x_pct": moondream_coords[0], "y_pct": moondream_coords[1], "text": desc, "type": "window"}
            method = "moondream_point_window"
        # Moondream 没返回才回退到 OmniParser 文字匹配
        if best is None:
            if text_ok:
                best = text_ok[0]; method = "omniparser_text_window"
            elif type_ok:
                best = type_ok[0]; method = "omniparser_type_window"

    # ── 非窗口类: OmniParser 文字优先 ──────────────────────
    if best is None:
        if text_ok:
            best = text_ok[0]; method = "omniparser_text"
        elif type_ok:
            best = type_ok[0]; method = "omniparser_type"

    # ── 通用回退: Moondream 辅助识别 ─────────────────────────
    hint = None
    if best is None and omni_ok:
        hint = _moondream_help_identify(desc, elements)
        if hint:
            hl = hint.lower()
            for e in elements:
                t = (e.get("text", "") or "").lower()
                if hl in t or any(w in t for w in hl.split()):
                    best = e; method = "moondream+omniparser"
                    break

    # ── 终极: Moondream point() ─────────────────────────────
    if best is None:
        if moondream_coords is None:
            moondream_coords = _moondream_point_coords(desc)
        if moondream_coords is not None:
            best = {"x_pct": moondream_coords[0], "y_pct": moondream_coords[1], "text": desc, "type": "moondream"}
            method = "moondream_point"

    # ── OmniParser 兜底 ────────────────────────────────────
    if best is None and omni_ok:
        for e in elements:
            if e.get("type") in ("button", "icon"):
                best = e; method = "omniparser_fallback"
                break

    if best is None:
        return {
            "error": (
                f"无法定位 '{desc}'。{len(elements)} 个元素。"
                + (f" Moondream: {hint}" if hint else "")
            )
        }

    # ── 点击 ────────────────────────────────────────────────
    try:
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        fx, fy = best["x_pct"], best["y_pct"]
        tx, ty = _to_pixels(fx, fy)
        tx, ty = _clamp(tx, ty)
        # 图标类元素自动双击（桌面图标/文件夹等）
        is_icon = (best.get("type") or "").lower() in ("icon", "图标") \
                  or not (best.get("text") or "").strip() \
                  or any(kw in desc.lower() for kw in ("icon", "图标", "桌面", "desktop", "file", "文件", "folder", "文件夹"))
        if is_icon:
            pg.doubleClick(x=tx, y=ty, button=(button or "left"))
            click_type = "double_clicked"
        else:
            pg.click(x=tx, y=ty, button=(button or "left"))
            click_type = "clicked"
        _track_position(tx, ty)
        return {
            "description": desc, "method": method,
            "matched": best.get("text") or best.get("type"),
            "x_pct": fx, "y_pct": fy, "x_pixel": tx, "y_pixel": ty,
            "button": button, "status": click_type,
        }
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
#  智能拖拽 — 按住 → 移动 → 松开（用于拖动窗口/文件/滑块）
# ═══════════════════════════════════════════════════════════════════════

@tool(
    name="mouse_smart_drag",
    description=(
        "Drag an element from one place to another. "
        "Uses Moondream for visual positioning (better for windows/dynamic elements).\n"
        "1. Find the drag handle using vision\n"
        "2. Press and hold\n"
        "3. Move to destination\n"
        "4. Release\n"
        "Example: mouse_smart_drag('the window title bar', end_landmark='center')\n"
        "Example: mouse_smart_drag('the slider thumb', end_x_pct=0.8, end_y_pct=0.5)\n"
        "For start position: use a description of the drag handle (title bar, icon, etc).\n"
        "For end position: use landmark, x_pct/y_pct, or another description."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_smart_drag(
    start_description: str,
    end_landmark: str | None = None,
    end_x_pct: float | None = None,
    end_y_pct: float | None = None,
    end_description: str | None = None,
    button: str = "left",
    duration: float = 0.5,
) -> dict[str, Any]:
    start_desc = (start_description or "").strip()
    if not start_desc:
        return {"error": "start_description 不能为空。"}

    pg = _get_pg()

    # ── 1. 找拖拽起点 ─────────────────────────────────────
    # 窗囗类优先用 Moondream point()
    start_coords = _moondream_point_coords(start_desc)
    if start_coords is not None:
        sx, sy = start_coords
        start_method = "moondream_point"
    else:
        # 回退 OmniParser
        omni = _call_omniparser(pg.screenshot())
        elements = omni.get("elements", []) if isinstance(omni, dict) else []
        q = start_desc.lower()
        match = None
        for e in elements:
            if q in (e.get("text", "") or "").lower():
                match = e; break
        if not match:
            for e in elements:
                if q in (e.get("type", "") or "").lower():
                    match = e; break
        if match:
            sx, sy = match["x_pct"], match["y_pct"]
            start_method = "omniparser"
        else:
            return {"error": f"无法找到拖拽起点 '{start_desc}'。"}

    # ── 2. 找拖拽终点 ─────────────────────────────────────
    if end_landmark is not None:
        ex, ey = _resolve(landmark=end_landmark)
        end_method = "landmark"
    elif end_x_pct is not None and end_y_pct is not None:
        ex, ey = _to_pixels(end_x_pct, end_y_pct)
        end_method = "pct"
    elif end_description is not None:
        end_coords = _moondream_point_coords(end_description)
        if end_coords is not None:
            ex, ey = _to_pixels(end_coords[0], end_coords[1])
            end_method = "moondream_point"
        else:
            return {"error": f"无法找到拖拽终点 '{end_description}'。"}
    else:
        return {"error": "必须指定 end_landmark、end_x_pct/end_y_pct 或 end_description。"}

    # ── 3. 执行拖拽: 按住 → 移动 → 松开 ──────────────────
    try:
        intr = _check_user_interrupt(pg)
        if intr:
            return intr
        sx_px, sy_px = _to_pixels(sx, sy)
        sx_px, sy_px = _clamp(sx_px, sy_px)
        ex, ey = _clamp(ex, ey)

        # 标题栏偏移修正：point() 识别窗口常偏下（点到内容区），
        # 向上挪 15px(~1% 屏高) 回到标题栏中央
        if any(kw in start_desc.lower() for kw in ("title", "标题", "window", "窗口", "bar", "栏", "drag", "拖", "tab", "标签")):
            sy_px -= max(10, int(_SCREEN_H * 0.012))

        _win32_drag(sx_px, sy_px, ex, ey, button=(button or "left"), duration=duration)
        _track_position(ex, ey)
        return {
            "start": [sx_px, sy_px], "end": [ex, ey],
            "start_method": start_method, "end_method": end_method,
            "button": button, "status": "dragged",
        }
    except ToolNotReady:
        raise
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
#  Moondream 定位工具 — 暴露 point() API 给 LLM 直接调
# ═══════════════════════════════════════════════════════════════════════

@tool(
    name="mouse_moondream_point",
    description=(
        "Use Moondream vision model to locate an element on screen and return its "
        "coordinates (x_pct, y_pct). Better than OmniParser for dynamic/window elements. "
        "Describe what you want to find in natural language.\n"
        "Example: mouse_moondream_point('the title bar of the topmost window')\n"
        "Returns x_pct/y_pct ready for mouse_click_at(x_pct=..., y_pct=...).\n"
        "NOTE: requires Moondream vision plugin loaded."
    ),
    group=MOUSE_TOOL_GROUP,
    risk="low",
)
def mouse_moondream_point(description: str) -> dict[str, Any]:
    desc = (description or "").strip()
    if not desc:
        return {"error": "description 不能为空。"}
    coords = _moondream_point_coords(desc)
    if coords is None:
        return {"error": f"Moondream point() 未能定位 '{desc}'。请确保 Moondream 插件已加载。"}
    return {
        "description": desc,
        "x_pct": round(coords[0], 5),
        "y_pct": round(coords[1], 5),
    }
