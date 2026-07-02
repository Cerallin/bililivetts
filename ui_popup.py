"""
弹窗窗口：独立 TTS 输入窗口 + 直播互动弹幕窗口。
直播互动窗口使用 Windows UpdateLayeredWindow 实现逐像素 alpha：
窗口背景可半透明，文字始终完全不透明。
底部输入区使用独立子窗承载标准 TextCtrl，确保中文输入法正常工作。
"""

import ctypes
from ctypes import wintypes

import wx
import time

from danmaku_history import DanmakuMessageList

LOG_FORMAT = "%Y-%m-%d %H:%M:%S"

# 深色主题颜色常量
COLOR_BG_DARK = "#0D0D0D"
COLOR_BG_PANEL = "#141414"
COLOR_BG_INPUT = "#1E1E1E"
COLOR_TITLE_BAR = "#161616"
COLOR_TEXT_WHITE = "#FFFFFF"
COLOR_TEXT_GRAY = "#888888"
COLOR_ACCENT = "#FF6699"
COLOR_STATS = "#AAAAAA"
COLOR_BORDER = "#2A2A2A"

# Win32 常量
_WS_EX_LAYERED = 0x00080000
_ULW_ALPHA = 0x00000002
_AC_SRC_ALPHA = 1
_GWL_EXSTYLE = -20
_BI_RGB = 0
_DIB_RGB_COLORS = 0

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_kernel32 = ctypes.windll.kernel32
_msimg32 = ctypes.windll.msimg32


# ---------------------------------------------------------------------------
# Win32 辅助：逐像素 alpha 分层窗口
# ---------------------------------------------------------------------------

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize",          wintypes.DWORD),
        ("biWidth",         wintypes.LONG),
        ("biHeight",        wintypes.LONG),
        ("biPlanes",        wintypes.WORD),
        ("biBitCount",      wintypes.WORD),
        ("biCompression",   wintypes.DWORD),
        ("biSizeImage",     wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed",       wintypes.DWORD),
        ("biClrImportant",  wintypes.DWORD),
    ]


def _create_dib(width, height):
    """创建 32-bit top-down DIB Section。返回 (hbitmap, pixel_ptr, width, height)。
    pixel_ptr 指向原始 BGRA 像素缓冲区（逐行连续，top-down 即第一行是顶部）。
    """
    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth = width
    bmi.biHeight = -height  # 负值 = top-down（第一行是顶部），无需翻转
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = _BI_RGB
    bmi.biSizeImage = width * height * 4

    ppv_bits = ctypes.c_void_p()
    screen_dc = _user32.GetDC(0)
    hbmp = _gdi32.CreateDIBSection(
        screen_dc,
        ctypes.byref(bmi),
        _DIB_RGB_COLORS,
        ctypes.byref(ppv_bits),
        None,
        0,
    )
    _user32.ReleaseDC(0, screen_dc)
    if not hbmp:
        raise RuntimeError("CreateDIBSection failed")
    return hbmp, ppv_bits, width, height


def _fill_dib_rect(pixel_ptr, dib_w, dib_h, x, y, w, h, r, g, b, a):
    """在 DIB 像素缓冲区中填充矩形区域（预乘 alpha），使用批量字节填充。"""
    if a <= 0 or w <= 0 or h <= 0:
        return
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(dib_w, x + w)
    y1 = min(dib_h, y + h)
    if x0 >= x1 or y0 >= y1:
        return

    pr = (r * a) // 255
    pg = (g * a) // 255
    pb = (b * a) // 255
    pa = a

    stride = dib_w * 4
    total = dib_w * dib_h * 4
    try:
        addr = ctypes.addressof(pixel_ptr.contents)
    except Exception:
        addr = ctypes.cast(pixel_ptr, ctypes.c_void_p).value
    buf = (ctypes.c_ubyte * total).from_address(addr)

    row_pixels = x1 - x0
    # 预构造一行 BGRA 字节序列
    row_data = bytes([pb, pg, pr, pa]) * row_pixels

    for row in range(y0, y1):
        start = row * stride + x0 * 4
        buf[start:start + row_pixels * 4] = row_data


# ---------------------------------------------------------------------------
# GDI+ 初始化 & 文字绘制辅助
# ---------------------------------------------------------------------------

_gdiplus_token = ctypes.c_void_p(0)
_gdiplus_initialized = False


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion",          wintypes.UINT),
        ("DebugEventCallback",      ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs",   wintypes.BOOL),
    ]


def _init_gdiplus():
    global _gdiplus_initialized, _gdiplus_token
    if _gdiplus_initialized:
        return True
    si = _GdiplusStartupInput()
    si.GdiplusVersion = 1
    result = ctypes.windll.gdiplus.GdiplusStartup(
        ctypes.byref(_gdiplus_token), ctypes.byref(si), None
    )
    if result == 0:
        _gdiplus_initialized = True
        return True
    return False


def _shutdown_gdiplus():
    global _gdiplus_initialized, _gdiplus_token
    if _gdiplus_initialized:
        ctypes.windll.gdiplus.GdiplusShutdown(_gdiplus_token)
        _gdiplus_initialized = False


class _RectF(ctypes.Structure):
    _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float),
                ("Width", ctypes.c_float), ("Height", ctypes.c_float)]


def _gdiplus_draw_text(hdc, text, x, y, font_name, font_size, bold, r, g, b):
    """使用 GDI+ 在指定 HDC 上绘制文字（alpha=255）。"""
    if not _gdiplus_initialized:
        return
    # 创建 GDI+ Graphics
    gp_graphics = ctypes.c_void_p()
    if ctypes.windll.gdiplus.GdipCreateFromHDC(hdc, ctypes.byref(gp_graphics)) != 0:
        return
    ctypes.windll.gdiplus.GdipSetTextRenderingHint(gp_graphics, 4)
    # SolidBrush
    argb = (255 << 24) | (b << 16) | (g << 8) | r
    gp_brush = ctypes.c_void_p()
    if ctypes.windll.gdiplus.GdipCreateSolidFill(argb, ctypes.byref(gp_brush)) != 0:
        ctypes.windll.gdiplus.GdipDeleteGraphics(gp_graphics)
        return
    # FontFamily
    gp_family = ctypes.c_void_p()
    if ctypes.windll.gdiplus.GdipCreateFontFamilyFromName(
        ctypes.c_wchar_p(font_name), None, ctypes.byref(gp_family)
    ) != 0:
        ctypes.windll.gdiplus.GdipDeleteBrush(gp_brush)
        ctypes.windll.gdiplus.GdipDeleteGraphics(gp_graphics)
        return
    # Font
    font_style = 1 if bold else 0
    gp_font = ctypes.c_void_p()
    if ctypes.windll.gdiplus.GdipCreateFont(
        gp_family, ctypes.c_float(
            font_size), font_style, 3, ctypes.byref(gp_font)
    ) != 0:
        ctypes.windll.gdiplus.GdipDeleteFontFamily(gp_family)
        ctypes.windll.gdiplus.GdipDeleteBrush(gp_brush)
        ctypes.windll.gdiplus.GdipDeleteGraphics(gp_graphics)
        return
    # StringFormat
    gp_format = ctypes.c_void_p()
    ctypes.windll.gdiplus.GdipCreateStringFormat(0, 0, ctypes.byref(gp_format))
    rect = _RectF(ctypes.c_float(x), ctypes.c_float(y), 10000.0, 100.0)
    ctypes.windll.gdiplus.GdipDrawString(
        gp_graphics, ctypes.c_wchar_p(text), -1,
        gp_font, ctypes.byref(rect), gp_format, gp_brush
    )
    ctypes.windll.gdiplus.GdipDeleteStringFormat(gp_format)
    ctypes.windll.gdiplus.GdipDeleteFont(gp_font)
    ctypes.windll.gdiplus.GdipDeleteFontFamily(gp_family)
    ctypes.windll.gdiplus.GdipDeleteBrush(gp_brush)
    ctypes.windll.gdiplus.GdipDeleteGraphics(gp_graphics)


# 缓存 GDI+ 对象的简单结构
class _GdiplusTextCache:
    """缓存 GDI+ Graphics / FontFamily / Font 对象，避免每帧重复创建。"""
    __slots__ = ('gp_graphics', 'gp_format', '_families', '_fonts')

    def __init__(self):
        self.gp_graphics = None
        self.gp_format = None
        self._families = {}   # (name,) → gp_family
        self._fonts = {}      # (name, size, bold) → gp_font

    def begin(self, hdc):
        self.gp_graphics = ctypes.c_void_p()
        if ctypes.windll.gdiplus.GdipCreateFromHDC(hdc, ctypes.byref(self.gp_graphics)) == 0:
            ctypes.windll.gdiplus.GdipSetTextRenderingHint(self.gp_graphics, 4)
        self.gp_format = ctypes.c_void_p()
        ctypes.windll.gdiplus.GdipCreateStringFormat(
            0, 0, ctypes.byref(self.gp_format))

    def get_family(self, name):
        key = (name,)
        if key not in self._families:
            gp = ctypes.c_void_p()
            ctypes.windll.gdiplus.GdipCreateFontFamilyFromName(
                ctypes.c_wchar_p(name), None, ctypes.byref(gp))
            self._families[key] = gp
        return self._families[key]

    def get_font(self, name, size, bold):
        key = (name, size, bold)
        if key not in self._fonts:
            family = self.get_family(name)
            gp = ctypes.c_void_p()
            style = 1 if bold else 0
            ctypes.windll.gdiplus.GdipCreateFont(
                family, ctypes.c_float(size), style, 3, ctypes.byref(gp))
            self._fonts[key] = gp
        return self._fonts[key]

    def draw(self, text, x, y, font_name, font_size, bold, r, g, b):
        if not self.gp_graphics:
            return
        argb = (255 << 24) | (b << 16) | (g << 8) | r
        gp_brush = ctypes.c_void_p()
        ctypes.windll.gdiplus.GdipCreateSolidFill(argb, ctypes.byref(gp_brush))
        gp_font = self.get_font(font_name, font_size, bold)
        rect = _RectF(ctypes.c_float(x), ctypes.c_float(y), 10000.0, 100.0)
        ctypes.windll.gdiplus.GdipDrawString(
            self.gp_graphics, ctypes.c_wchar_p(text), -1,
            gp_font, ctypes.byref(rect), self.gp_format, gp_brush
        )
        ctypes.windll.gdiplus.GdipDeleteBrush(gp_brush)

    def measure_height(self, text, max_width, font_name, font_size, bold):
        """测量文本在指定宽度内换行后的像素高度。"""
        if not self.gp_graphics:
            return 22
        gp_font = self.get_font(font_name, font_size, bold)
        layout_rect = _RectF(0.0, 0.0, ctypes.c_float(max_width), 10000.0)
        out_rect = _RectF()
        ctypes.windll.gdiplus.GdipMeasureString(
            self.gp_graphics, ctypes.c_wchar_p(text), -1,
            gp_font, ctypes.byref(layout_rect), self.gp_format,
            ctypes.byref(out_rect), None, None
        )
        return int(out_rect.Height + 0.5)

    def measure_width(self, text, font_name, font_size, bold):
        """测量单行文本的像素宽度。"""
        if not self.gp_graphics or not text:
            return 0
        gp_font = self.get_font(font_name, font_size, bold)
        layout_rect = _RectF(0.0, 0.0, 10000.0, 100.0)
        out_rect = _RectF()
        ctypes.windll.gdiplus.GdipMeasureString(
            self.gp_graphics, ctypes.c_wchar_p(text), -1,
            gp_font, ctypes.byref(layout_rect), self.gp_format,
            ctypes.byref(out_rect), None, None
        )
        return int(out_rect.Width + 0.5)

    def draw_wrapped(self, text, x, y, max_width, font_name, font_size,
                     bold, r, g, b):
        """在指定宽度内绘制自动换行的文本。"""
        if not self.gp_graphics:
            return
        argb = (255 << 24) | (b << 16) | (g << 8) | r
        gp_brush = ctypes.c_void_p()
        ctypes.windll.gdiplus.GdipCreateSolidFill(argb, ctypes.byref(gp_brush))
        gp_font = self.get_font(font_name, font_size, bold)
        rect = _RectF(ctypes.c_float(x), ctypes.c_float(y),
                      ctypes.c_float(max_width), 10000.0)
        ctypes.windll.gdiplus.GdipDrawString(
            self.gp_graphics, ctypes.c_wchar_p(text), -1,
            gp_font, ctypes.byref(rect), self.gp_format, gp_brush
        )
        ctypes.windll.gdiplus.GdipDeleteBrush(gp_brush)

    def draw_line(self, x1, y1, x2, y2, r, g, b, a=255, width=1.0):
        """在指定 HDC 上绘制一条直线（使用 GDI+ Pen）。"""
        if not self.gp_graphics:
            return
        argb = (a << 24) | (b << 16) | (g << 8) | r
        gp_pen = ctypes.c_void_p()
        ctypes.windll.gdiplus.GdipCreatePen1(
            argb, ctypes.c_float(width), 2, ctypes.byref(gp_pen))
        ctypes.windll.gdiplus.GdipDrawLine(
            self.gp_graphics, gp_pen,
            ctypes.c_float(x1), ctypes.c_float(y1),
            ctypes.c_float(x2), ctypes.c_float(y2))
        ctypes.windll.gdiplus.GdipDeletePen(gp_pen)

    def end(self):
        for gp in self._fonts.values():
            ctypes.windll.gdiplus.GdipDeleteFont(gp)
        for gp in self._families.values():
            ctypes.windll.gdiplus.GdipDeleteFontFamily(gp)
        if self.gp_format:
            ctypes.windll.gdiplus.GdipDeleteStringFormat(self.gp_format)
        if self.gp_graphics:
            ctypes.windll.gdiplus.GdipDeleteGraphics(self.gp_graphics)
        self._fonts.clear()
        self._families.clear()
        self.gp_graphics = None
        self.gp_format = None


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp",     ctypes.c_byte),
        ("BlendFlags",  ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


def _make_layered(hwnd):
    """为窗口附加 WS_EX_LAYERED 样式。"""
    style = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    _user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style | _WS_EX_LAYERED)


def _update_layered(hwnd, bitmap, x, y):
    """使用 per-pixel alpha bitmap 刷新分层窗口。bitmap 可以是 wx.Bitmap 或原始 HBITMAP。"""
    if isinstance(bitmap, wx.Bitmap):
        w = bitmap.GetWidth()
        h = bitmap.GetHeight()
        bmp_handle = bitmap.GetHandle()
    else:
        # 假设是 (hbmp, w, h) 元组
        hbmp, w, h = bitmap
        bmp_handle = hbmp
    if w <= 0 or h <= 0 or not bmp_handle:
        return

    screen_dc = _user32.GetDC(0)
    mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
    old_bmp = _gdi32.SelectObject(mem_dc, bmp_handle)

    blend = _BLENDFUNCTION(0, 0, 255, _AC_SRC_ALPHA)
    pt_src = _POINT(0, 0)
    pt_dest = _POINT(x, y)
    size = _SIZE(w, h)

    _user32.UpdateLayeredWindow(
        hwnd,
        screen_dc,
        ctypes.byref(pt_dest),
        ctypes.byref(size),
        mem_dc,
        ctypes.byref(pt_src),
        0,
        ctypes.byref(blend),
        _ULW_ALPHA,
    )

    _gdi32.SelectObject(mem_dc, old_bmp)
    _gdi32.DeleteDC(mem_dc)
    _user32.ReleaseDC(0, screen_dc)


# ---------------------------------------------------------------------------
# PopupTtsFrame（保留原有简单弹窗）
# ---------------------------------------------------------------------------

class PopupTtsFrame(wx.Frame):
    """原有的独立 TTS 输入弹窗。"""

    def __init__(self, parent, on_send):
        super().__init__(
            parent,
            title="独立 TTS 窗口",
            size=(380, 120),
            style=wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP,
        )
        self.SetBackgroundColour(COLOR_BG_DARK)
        self.SetTransparent(230)

        panel = wx.Panel(self)
        panel.SetBackgroundColour(COLOR_BG_DARK)
        content_sizer = wx.BoxSizer(wx.VERTICAL)

        self.input_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_PROCESS_ENTER | wx.BORDER_SIMPLE,
        )
        self.input_ctrl.SetBackgroundColour(COLOR_BG_INPUT)
        self.input_ctrl.SetForegroundColour(COLOR_TEXT_WHITE)
        self.input_ctrl.SetFont(
            wx.Font(
                10,
                wx.FONTFAMILY_SWISS,
                wx.FONTSTYLE_NORMAL,
                wx.FONTWEIGHT_NORMAL,
                False,
                "微软雅黑",
            )
        )

        content_sizer.Add(self.input_ctrl, 1, wx.ALL | wx.EXPAND, 8)

        panel.SetSizer(content_sizer)

        outer_sizer = wx.BoxSizer(wx.VERTICAL)
        outer_sizer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer_sizer)

        self.on_send = on_send
        self.input_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_enter)

    def _on_enter(self, event):
        text = self.input_ctrl.GetValue().strip()
        if text:
            self.input_ctrl.SetValue("")
            self.on_send(text)


# ---------------------------------------------------------------------------
# DanmakuInteractionFrame — 逐像素 alpha 直播互动窗
# ---------------------------------------------------------------------------

class DanmakuInteractionFrame(wx.MiniFrame):
    """
    直播互动弹幕窗口：
    - 使用 UpdateLayeredWindow 实现窗口背景透明、文字完全不透明。
    - 底部输入区使用独立子窗（wx.Frame），确保中文输入法正常工作。
    - 自定义深色标题栏（仅关闭按钮），支持拖拽移动。
    """

    MAX_INPUT_LENGTH = 40
    TITLE_HEIGHT = 36
    INPUT_HEIGHT = 38
    DEFAULT_OPACITY = 200  # 背景透明度 0-255，越小越透明
    RESIZE_BORDER = 6       # 边缘拖拽调整大小的像素距离
    MIN_WIDTH = 200
    MIN_HEIGHT = 150

    def __init__(self, parent, on_send, on_close_cb,
                 initial_width=400, initial_height=520):
        # 必须是真正的顶层窗口（parent=None），否则 GetPosition 返回相对坐标，
        # 与 UpdateLayeredWindow 需要的屏幕坐标不一致，导致位置错乱。
        style = wx.BORDER_NONE | wx.STAY_ON_TOP | wx.FRAME_NO_TASKBAR
        super().__init__(None, title="直播互动", style=style)

        self._send_callback = on_send
        self._on_close_cb = on_close_cb
        self._drag_mode = None       # None | 'move' | 'N'|'S'|'E'|'W'|'NE'|'NW'|'SE'|'SW'
        self._drag_start_mouse = None
        self._online_count = 0
        self._bg_opacity = self.DEFAULT_OPACITY
        self._msg_list = DanmakuMessageList()
        self._input_focused = False
        self._hover_close = False
        self._needs_render = True  # 脏标记，避免无效渲染
        self._cursor_visible = True  # 光标闪烁状态
        self._cursor_tick = 0  # 光标闪烁计数器（每 10 tick = 500ms 翻转一次）
        self._last_insertion_point = 0  # 追踪插入点变化，变化时重置光标可见
        self._clicked_in_window = False  # 追踪用户是否在本窗口内点击，用于 _on_activate

        # 窗口尺寸（可使用上次保存的值）
        self._width = max(self.MIN_WIDTH, initial_width)
        self._height = max(self.MIN_HEIGHT, initial_height)
        # 屏幕位置（由 UpdateLayeredWindow 控制，需自行追踪，不能依赖 GetScreenPosition）
        self._screen_x = 0
        self._screen_y = 0

        # -------- 创建底部输入子窗（独立顶层窗口，不透明）--------
        # 注意：不能作为主窗的子窗口，因为分层窗口的子窗口渲染有问题。
        self._input_frame = wx.MiniFrame(
            self,
            title="",
            style=wx.BORDER_NONE | wx.STAY_ON_TOP | wx.FRAME_NO_TASKBAR,
            size=(self._width, self.INPUT_HEIGHT),
        )
        self._input_frame.SetBackgroundColour(COLOR_BG_PANEL)
        self._build_input_ui()
        self._input_frame.Show()

        # -------- 让主窗成为分层窗口 --------
        _make_layered(self.GetHandle())
        # -------- 初始化 GDI+（用于文字绘制）--------
        _init_gdiplus()

        # 字体
        self._title_font = wx.Font(
            10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL,
            wx.FONTWEIGHT_BOLD, False, "微软雅黑"
        )
        self._close_font = wx.Font(
            12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL,
            wx.FONTWEIGHT_BOLD, False, "微软雅黑"
        )
        self._stats_font = wx.Font(
            9, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL,
            wx.FONTWEIGHT_NORMAL, False, "微软雅黑"
        )
        # 初始位置 & 渲染
        self.SetClientSize(self._width, self._height)
        self.Centre()
        # 记录 UpdateLayeredWindow 使用的屏幕坐标
        self._screen_x, self._screen_y = self.GetScreenPosition()
        self._render_and_update()
        self._sync_input_frame_pos()

        # 事件
        self._bind_events()

        # 渲染定时器（50ms 高刷，光标闪烁 500ms 周期，输入即时渲染）
        self._render_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_render_timer, self._render_timer)
        self._render_timer.Start(50)

    # ==================================================================
    # 底部输入子窗 UI
    # ==================================================================

    def _build_input_ui(self):
        panel = wx.Panel(self._input_frame)
        panel.SetBackgroundColour(COLOR_BG_PANEL)

        self._input_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_PROCESS_ENTER | wx.BORDER_NONE,
            size=(0, 28),
        )
        self._input_ctrl.SetBackgroundColour(COLOR_BG_INPUT)
        self._input_ctrl.SetForegroundColour(COLOR_TEXT_WHITE)
        self._input_ctrl.SetFont(
            wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL,
                    wx.FONTWEIGHT_NORMAL, False, "微软雅黑")
        )
        self._input_ctrl.SetHint("请输入文字")
        self._input_ctrl.SetMinSize((0, 24))

        self._char_count = wx.StaticText(panel, label="0/40")
        self._char_count.SetForegroundColour(COLOR_TEXT_GRAY)
        self._char_count.SetFont(
            wx.Font(8, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL,
                    wx.FONTWEIGHT_NORMAL, False, "微软雅黑")
        )

        input_sizer = wx.BoxSizer(wx.HORIZONTAL)
        input_sizer.Add(self._input_ctrl, 1,
                        wx.ALIGN_CENTER_VERTICAL | wx.ALL, 4)
        input_sizer.Add(self._char_count, 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        panel.SetSizer(input_sizer)

        frame_sizer = wx.BoxSizer(wx.VERTICAL)
        frame_sizer.Add(panel, 1, wx.EXPAND)
        self._input_frame.SetSizer(frame_sizer)

        self._input_frame.Layout()

        # 使输入子窗近乎不可见（仅保留交互/IME 能力），文字由主窗 DIB 手绘
        _make_layered(self._input_frame.GetHandle())
        _user32.SetLayeredWindowAttributes(
            self._input_frame.GetHandle(), 0, 1, 0x02)

        # 输入子窗事件
        self._input_ctrl.Bind(wx.EVT_TEXT, self._on_input_change)
        self._input_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_send_click)
        self._input_ctrl.Bind(wx.EVT_SET_FOCUS, self._on_input_focus_in)
        self._input_ctrl.Bind(wx.EVT_KILL_FOCUS, self._on_input_focus_out)
        self._input_ctrl.Bind(wx.EVT_KEY_UP, self._on_input_key_up)

        # 面板边缘调整大小事件（底部边缘由输入框面板覆盖，需在此处理）
        panel.Bind(wx.EVT_LEFT_DOWN, self._on_input_panel_left_down)
        panel.Bind(wx.EVT_MOTION, self._on_input_panel_motion)
        panel.Bind(wx.EVT_LEFT_UP, self._on_input_panel_left_up)

    def _sync_input_frame_pos(self):
        """将输入子窗定位到主窗底部，并确保其在主窗上方。
        使用 Win32 SetWindowPos 而非 wx.Frame.SetSize，避免 wx.Frame
        因窗口样式（如 WS_EX_WINDOWEDGE）产生额外的像素偏差。
        """
        try:
            hwnd_input = self._input_frame.GetHandle()
            _user32.SetWindowPos(
                hwnd_input, 0,
                self._screen_x, self._screen_y + self._height - self.INPUT_HEIGHT,
                self._width, self.INPUT_HEIGHT,
                0x0004 | 0x0010,  # SWP_NOZORDER | SWP_NOACTIVATE
            )
            self._input_frame.Refresh()
        except Exception:
            pass

    # ==================================================================
    # 主窗事件
    # ==================================================================

    def _bind_events(self):
        # 标题栏拖拽
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)

        # 窗口大小变化 → 同步输入子窗位置
        self.Bind(wx.EVT_SIZE, self._on_size)

        # 鼠标滚轮 → 消息区滚动
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_mouse_wheel)

        # 键盘 → 当输入区聚焦时由子窗处理；否则忽略
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ----- 窗口大小变化 -----
    def _on_size(self, event):
        self._needs_render = True
        event.Skip()

    # ----- 拖拽 / 点击 -----

    def _on_left_down(self, event):
        x, y = event.GetX(), event.GetY()
        cw = self._width
        ch = self._height

        # 关闭按钮
        if y <= self.TITLE_HEIGHT and x >= cw - 36 and x <= cw:
            self.Close()
            return

        # 边缘调整大小（优先级高于标题栏拖拽）
        mode = self._get_resize_mode(x, y)
        if mode is not None:
            self._drag_mode = mode
            self._drag_start_mouse = wx.GetMousePosition()
            if not self.HasCapture():
                self.CaptureMouse()
            return

        # 标题栏拖拽移动
        if y <= self.TITLE_HEIGHT:
            self._drag_mode = 'move'
            self._drag_start_mouse = wx.GetMousePosition()
            if not self.HasCapture():
                self.CaptureMouse()
        else:
            self._clicked_in_window = True  # 用户在消息区点击，允许后续自动聚焦
            self._focus_input()
            event.Skip()

    def _focus_input(self):
        """将焦点转移到底部输入框。"""
        try:
            self._input_ctrl.SetFocus()
            self._input_frame.Raise()
        except Exception:
            pass

    def _on_activate(self, event):
        """窗口被激活时自动聚焦输入框。"""
        if event.GetActive() and self._clicked_in_window:
            wx.CallAfter(self._focus_input)
            self._clicked_in_window = False
        event.Skip()

    def _on_left_up(self, event):
        if self._drag_mode is not None:
            self._drag_mode = None
            self._drag_start_mouse = None
            if self.HasCapture():
                self.ReleaseMouse()
            self.SetCursor(wx.Cursor(wx.CURSOR_ARROW))

    def _on_motion(self, event):
        x, y = event.GetX(), event.GetY()
        cw = self._width
        ch = self._height

        # 正在拖拽（移动或调整大小）
        if self._drag_mode is not None and event.Dragging() and event.LeftIsDown():
            cur_mouse = wx.GetMousePosition()
            delta = cur_mouse - self._drag_start_mouse
            self._drag_start_mouse = cur_mouse
            if self._drag_mode == 'move':
                self._screen_x += delta.x
                self._screen_y += delta.y
            else:
                self._do_resize(self._drag_mode, delta.x, delta.y)
            self._render_and_update()
            self._sync_input_frame_pos()
            return

        # 关闭按钮 hover
        hover = (y <= self.TITLE_HEIGHT and x >= cw - 36 and x <= cw)
        if hover != self._hover_close:
            self._hover_close = hover
            self._needs_render = True

        # 光标样式（边缘调整大小）
        mode = self._get_resize_mode(x, y)
        if mode:
            cursors = {
                'N': wx.CURSOR_SIZENS, 'S': wx.CURSOR_SIZENS,
                'E': wx.CURSOR_SIZEWE, 'W': wx.CURSOR_SIZEWE,
                'NE': wx.CURSOR_SIZENESW, 'SW': wx.CURSOR_SIZENESW,
                'NW': wx.CURSOR_SIZENWSE, 'SE': wx.CURSOR_SIZENWSE,
            }
            self.SetCursor(wx.Cursor(cursors[mode]))
        else:
            self.SetCursor(wx.Cursor(wx.CURSOR_ARROW))

    def _on_capture_lost(self, event):
        self._drag_mode = None
        self._drag_start_mouse = None

    # ----- 边缘调整大小辅助 -----

    def _get_resize_mode(self, x, y):
        """根据鼠标在窗口内的相对位置返回调整大小方向。"""
        cw = self._width
        ch = self._height
        b = self.RESIZE_BORDER
        mode = ''
        if x < b:
            mode += 'W'
        elif x > cw - b:
            mode += 'E'
        if y < b:
            mode += 'N'
        elif y > ch - b:
            mode += 'S'
        return mode if mode else None

    def _do_resize(self, mode, dx, dy):
        """根据拖拽模式与鼠标增量调整窗口大小及屏幕位置。"""
        new_w, new_h = self._width, self._height
        new_x, new_y = self._screen_x, self._screen_y

        if 'E' in mode:
            new_w = max(self.MIN_WIDTH, new_w + dx)
        if 'W' in mode:
            old_w = new_w
            new_w = max(self.MIN_WIDTH, new_w - dx)
            new_x += old_w - new_w
        if 'S' in mode:
            new_h = max(self.MIN_HEIGHT, new_h + dy)
        if 'N' in mode:
            old_h = new_h
            new_h = max(self.MIN_HEIGHT, new_h - dy)
            new_y += old_h - new_h

        if new_w != self._width or new_h != self._height:
            self._width = new_w
            self._height = new_h
            self.SetClientSize(new_w, new_h)

        self._screen_x = new_x
        self._screen_y = new_y

    # ----- 输入框面板边缘调整大小（底部边缘由输入框覆盖，需单独处理）-----

    def _on_input_panel_left_down(self, event):
        x, y = event.GetX(), event.GetY()
        pw = self._input_frame.GetSize().x
        ph = self.INPUT_HEIGHT
        b = self.RESIZE_BORDER

        mode = ''
        if x < b:
            mode += 'W'
        elif x > pw - b:
            mode += 'E'
        if y > ph - b:
            mode += 'S'

        if 'S' in mode:
            self._drag_mode = mode
            self._drag_start_mouse = wx.GetMousePosition()
            self._input_frame.CaptureMouse()
        else:
            event.Skip()

    def _on_input_panel_motion(self, event):
        if self._drag_mode is not None and 'S' in self._drag_mode \
                and event.Dragging() and event.LeftIsDown():
            cur_mouse = wx.GetMousePosition()
            delta = cur_mouse - self._drag_start_mouse
            self._drag_start_mouse = cur_mouse
            self._do_resize(self._drag_mode, delta.x, delta.y)
            self._render_and_update()
            self._sync_input_frame_pos()
            return

        x, y = event.GetX(), event.GetY()
        pw = self._input_frame.GetSize().x
        ph = self.INPUT_HEIGHT
        b = self.RESIZE_BORDER

        mode = ''
        if x < b:
            mode += 'W'
        elif x > pw - b:
            mode += 'E'
        if y > ph - b:
            mode += 'S'

        if mode and 'S' in mode:
            cursors = {
                'S': wx.CURSOR_SIZENS,
                'SW': wx.CURSOR_SIZENESW,
                'SE': wx.CURSOR_SIZENWSE,
            }
            self._input_frame.SetCursor(wx.Cursor(cursors[mode]))
        else:
            self._input_frame.SetCursor(wx.Cursor(wx.CURSOR_ARROW))
        event.Skip()

    def _on_input_panel_left_up(self, event):
        if self._drag_mode is not None and 'S' in self._drag_mode:
            self._drag_mode = None
            self._drag_start_mouse = None
            if self._input_frame.HasCapture():
                self._input_frame.ReleaseMouse()
            self._input_frame.SetCursor(wx.Cursor(wx.CURSOR_ARROW))
        else:
            event.Skip()

    # ----- 鼠标滚轮滚动消息 -----

    def _on_mouse_wheel(self, event):
        y = event.GetY()
        msg_top = self.TITLE_HEIGHT
        msg_bottom = self.GetSize().y - self.INPUT_HEIGHT
        if msg_top <= y <= msg_bottom:
            delta = event.GetWheelRotation() // event.GetWheelDelta() * 24
            self._msg_list.scroll(delta)
            self._needs_render = True

    # ----- 关闭 -----

    def _on_close(self, event):
        try:
            self._render_timer.Stop()
        except Exception:
            pass
        try:
            if getattr(self, '_prev_hbmp', None):
                _gdi32.DeleteObject(self._prev_hbmp)
                self._prev_hbmp = None
        except Exception:
            pass
        try:
            self._input_frame.Destroy()
        except Exception:
            pass
        try:
            self._on_close_cb()
        except Exception:
            pass
        self.Destroy()

    # ----- 定时渲染 -----

    def _on_render_timer(self, event):
        # 光标闪烁（每 10 tick = 500ms 翻转一次）
        self._cursor_tick += 1
        if self._cursor_tick >= 10:
            self._cursor_tick = 0
            self._cursor_visible = not self._cursor_visible
        # 检测插入点是否因鼠标点击等改变了（非按键触发的情况）
        if self._input_focused:
            try:
                new_ip = self._input_ctrl.GetInsertionPoint()
                if new_ip != self._last_insertion_point:
                    self._last_insertion_point = new_ip
                    self._cursor_visible = True
                    self._cursor_tick = 0
                    self._needs_render = True
            except Exception:
                pass
        # 聚焦时持续刷新（光标闪烁），或有脏标记时渲染
        if self._needs_render or self._input_focused:
            self._needs_render = False
            self._render_and_update()

    # ==================================================================
    # 渲染：CreateDIBSection → 填矩形（预乘 alpha）→ GDI+ 绘文字 → UpdateLayeredWindow
    # ==================================================================

    def _render_and_update(self):
        sz = self.GetSize()
        cw, ch = sz.x, sz.y
        if cw <= 0 or ch <= 0:
            return
        self._width = cw
        self._height = ch

        alpha = self._bg_opacity

        # ---------- 1. 创建 32-bit DIB Section ----------
        hbmp, pixel_ptr, _, _ = _create_dib(cw, ch)

        # ---------- 2. 填背景（批量字节填充）----------
        _fill_dib_rect(pixel_ptr, cw, ch, 0, 0, cw,
                       ch, 0x0D, 0x0D, 0x0D, alpha)
        _fill_dib_rect(pixel_ptr, cw, ch, 0, 0, cw,
                       self.TITLE_HEIGHT, 0x16, 0x16, 0x16, alpha)

        msg_y = self.TITLE_HEIGHT
        msg_h = ch - msg_y - self.INPUT_HEIGHT
        input_y = ch - self.INPUT_HEIGHT

        if msg_h > 0:
            _fill_dib_rect(pixel_ptr, cw, ch, 0, msg_y, cw,
                           msg_h, 0x0D, 0x0D, 0x0D, alpha)

        # ---------- 3. 用 GDI+ 缓存绘制文字 ----------
        screen_dc = _user32.GetDC(0)
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        old_bmp = _gdi32.SelectObject(mem_dc, hbmp)

        tc = _GdiplusTextCache()
        tc.begin(mem_dc)

        # 标题
        tc.draw("直播互动", 12, 8, "微软雅黑", 10, True, 255, 255, 255)
        # 关闭按钮
        cr, cg, cb = (255, 80, 80) if self._hover_close else (0x88, 0x88, 0x88)
        tc.draw("✕", cw - 30, 6, "微软雅黑", 12, True, cr, cg, cb)
        # 消息区
        if msg_h > 0:
            if self._msg_list.is_empty:
                tc.draw("展示本场直播的弹幕互动消息", 12, msg_y + 10,
                        "微软雅黑", 10, False, 255, 255, 255)
            else:
                self._msg_list.draw(tc, msg_y + 4, cw, msg_h)

            # 分隔线（消息区与输入区之间，水平居中，宽度90%）
            line_margin = int(cw * 0.05)
            tc.draw_line(line_margin, input_y, cw - line_margin, input_y,
                         255, 255, 255, 80, 1.0)

        # 输入区文字（纯白，完全不透明，不受窗口透明度影响）
        input_text = self._input_ctrl.GetValue()
        # placeholder 颜色随背景透明度变化
        ph_gray = int(((255 - 128) * self._bg_opacity / 255) + 128)
        try:
            ip = self._input_ctrl.GetInsertionPoint()
        except Exception:
            ip = len(input_text)
        if input_text:
            tc.draw(input_text, 8, input_y + 7,
                    "微软雅黑", 10, False, 255, 255, 255)
            # 光标（根据实际插入点位置，而非始终在末尾）
            if self._cursor_visible and self._input_focused:
                prefix = input_text[:ip]
                text_w = tc.measure_width(prefix, "微软雅黑", 10, False)
                cursor_x = 8 + text_w + 2
                tc.draw_line(cursor_x, input_y + 6,
                             cursor_x, input_y + 28,
                             255, 255, 255, self._bg_opacity, 2.0)
        elif self._input_focused:
            # 聚焦无文字：placeholder 始终显示，光标叠加闪烁
            tc.draw("请输入文字", 8, input_y + 7,
                    "微软雅黑", 10, False, ph_gray, ph_gray, ph_gray)
            if self._cursor_visible:
                tc.draw_line(10, input_y + 6, 10, input_y + 28,
                             255, 255, 255, self._bg_opacity, 2.0)
        else:
            # 未聚焦无文字：仅 placeholder
            tc.draw("请输入文字", 8, input_y + 7,
                    "微软雅黑", 10, False, ph_gray, ph_gray, ph_gray)

        tc.end()

        _gdi32.SelectObject(mem_dc, old_bmp)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(0, screen_dc)

        # ---------- 4. 提交 ----------
        _update_layered(self.GetHandle(), (hbmp, cw, ch),
                        self._screen_x, self._screen_y)
        if getattr(self, '_prev_hbmp', None):
            _gdi32.DeleteObject(self._prev_hbmp)
        self._prev_hbmp = hbmp

    # ==================================================================
    # 输入子窗事件
    # ==================================================================

    def _on_input_change(self, event):
        text = self._input_ctrl.GetValue()
        length = len(text)
        self._char_count.SetLabel(f"{length}/{self.MAX_INPUT_LENGTH}")

        if length > self.MAX_INPUT_LENGTH:
            self._input_ctrl.SetValue(text[:self.MAX_INPUT_LENGTH])
            self._input_ctrl.SetInsertionPoint(self.MAX_INPUT_LENGTH)
            self._char_count.SetLabel(
                f"{self.MAX_INPUT_LENGTH}/{self.MAX_INPUT_LENGTH}")
        # 文本变化时插入点通常后移，重置光标闪烁
        try:
            self._last_insertion_point = self._input_ctrl.GetInsertionPoint()
        except Exception:
            pass
        self._cursor_visible = True
        self._cursor_tick = 0
        self._needs_render = True
        self._render_and_update()  # 即时渲染，消除输入延迟
        event.Skip()

    def _on_send_click(self, event):
        text = self._input_ctrl.GetValue().strip()
        if text:
            self._input_ctrl.SetValue("")
            self._char_count.SetLabel("0/40")
            self._last_insertion_point = 0
            self._send_callback(text)

    def _on_input_focus_in(self, event):
        self._input_focused = True
        self._cursor_visible = True
        self._needs_render = True
        event.Skip()

    def _on_input_focus_out(self, event):
        self._input_focused = False
        self._needs_render = True
        event.Skip()

    def _on_input_key_up(self, event):
        """按键抬起时检测插入点是否移动，立即刷新光标位置并重置闪烁。"""
        key = event.GetKeyCode()
        # 方向键、Home/End、鼠标点击等会改变插入点
        if key in (wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_UP, wx.WXK_DOWN,
                   wx.WXK_HOME, wx.WXK_END):
            try:
                new_ip = self._input_ctrl.GetInsertionPoint()
                if new_ip != self._last_insertion_point:
                    self._last_insertion_point = new_ip
                    self._cursor_visible = True
                    self._cursor_tick = 0
                    self._needs_render = True
            except Exception:
                pass
        event.Skip()

    # ==================================================================
    # 公开方法
    # ==================================================================

    def add_danmaku(self, uid, uname, content):
        """向消息区追加一条弹幕消息。"""
        self._msg_list.add(uname, content)
        self._needs_render = True

    def clear_messages(self):
        """清空消息区域。"""
        self._msg_list.clear()
        self._needs_render = True

    def set_online_count(self, count):
        """更新在线人数。"""
        self._online_count = count
        self._needs_render = True

    def set_bg_opacity(self, alpha):
        """设置背景透明度 (0-255)，仅影响窗口背景，文字始终不透明。"""
        self._bg_opacity = max(30, min(255, int(alpha)))
        self._needs_render = True
        # 输入子窗保持近乎不可见，文字由主窗 DIB 手绘

    def set_on_send_callback(self, on_send):
        self._send_callback = on_send

    def get_window_size(self):
        """返回当前窗口的 (width, height)，用于持久化。"""
        return (self._width, self._height)

    # ==================================================================
    # 覆写：移动 / 显示时同步输入子窗
    # ==================================================================

    def Move(self, pos, flags=wx.SIZE_USE_EXISTING):
        """更新自追踪位置，重新渲染（不依赖 wx 底层 Move，因为 UpdateLayeredWindow 控制实际位置）。"""
        self._screen_x = pos.x
        self._screen_y = pos.y
        self._render_and_update()
        self._sync_input_frame_pos()

    def SetPosition(self, pos):
        self._screen_x = pos.x
        self._screen_y = pos.y
        self._render_and_update()
        self._sync_input_frame_pos()

    def Show(self, show=True):
        super().Show(show)
        try:
            if show:
                self._input_frame.Show(True)
                self._sync_input_frame_pos()
            else:
                self._input_frame.Show(False)
        except Exception:
            pass

    def Raise(self):
        super().Raise()
        try:
            self._input_frame.Raise()
        except Exception:
            pass
