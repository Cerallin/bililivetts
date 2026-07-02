"""
Danmaku 历史消息组件。
管理消息存储、滚动状态，并通过 GDI+ 文本缓存进行自底向上的逐行渲染。
支持长弹幕自动换行。
"""


class DanmakuMessageList:
    """存储并绘制直播互动窗的弹幕消息。

    消息自底向上排列：最新消息始终出现在可视区域底部，
    旧消息向上滚动直至移出视野。长消息根据视口宽度自动换行。
    """

    MAX_MESSAGES = 200
    LINE_HEIGHT = 22

    def __init__(self):
        self._messages = []         # [(uid, content), ...]
        self._scroll_offset = 0     # 从底部向上滚动的像素数（0 = 停在最底部）
        self._max_scroll = 0        # 最大可滚动像素
        self._auto_scroll = True    # 新消息到来时自动滚到底部
        self._heights_dirty = True  # 高度缓存是否需要重新计算
        self._msg_heights = []      # 每条消息的像素高度（缓存）

    # ------------------------------------------------------------------
    # 消息管理
    # ------------------------------------------------------------------

    def add(self, username, content):
        """追加一条消息并触发自动滚底。"""
        self._messages.append((username, content))
        if len(self._messages) > self.MAX_MESSAGES:
            self._messages = self._messages[-self.MAX_MESSAGES:]
        self._heights_dirty = True
        self._auto_scroll = True

    def clear(self):
        """清空全部消息并重置滚动状态。"""
        self._messages.clear()
        self._msg_heights.clear()
        self._heights_dirty = True
        self._scroll_offset = 0
        self._max_scroll = 0
        self._auto_scroll = True

    @property
    def is_empty(self):
        return len(self._messages) == 0

    # ------------------------------------------------------------------
    # 滚动
    # ------------------------------------------------------------------

    def scroll(self, delta):
        """滚动 delta 像素。正数 = 向上滚（看更旧的消息）。"""
        self._scroll_offset = max(0, min(self._max_scroll,
                                         self._scroll_offset + delta))
        # 若用户手动滚回最底部，则重新启用自动滚底
        self._auto_scroll = (self._scroll_offset == 0)

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def draw(self, tc, start_y, width, msg_h):
        """在 GDI+ 文本缓存上绘制当前可见的消息。

        参数:
            tc:       _GdiplusTextCache 实例。
            start_y:  消息视口顶部 Y 坐标。
            width:    视口宽度（像素），用于长文本换行。
            msg_h:    视口高度（像素）。

        返回:
            bool: 是否实际绘制了消息。
        """
        if not self._messages:
            return False

        padding_x = 12
        text_width = max(width - padding_x * 2, 60)

        # 重新计算每条消息的换行高度
        if self._heights_dirty or len(self._msg_heights) != len(self._messages):
            self._msg_heights = []
            for username, content in self._messages:
                text = f"{username}：{content}"
                h = tc.measure_height(text, text_width,
                                      "微软雅黑", 10, False)
                self._msg_heights.append(max(h, self.LINE_HEIGHT))
            self._heights_dirty = False

        content_height = sum(self._msg_heights)
        self._max_scroll = max(0, content_height - msg_h)

        # 自动滚底
        if self._auto_scroll:
            self._scroll_offset = 0
            self._auto_scroll = False

        # 钳制滚动值
        self._scroll_offset = max(0, min(self._max_scroll,
                                         self._scroll_offset))

        bottom_y = start_y + msg_h

        # 从最新消息底部开始向上排列
        y = bottom_y + self._scroll_offset

        # 从最新到最旧逐条绘制（自底向上）
        for i in range(len(self._messages) - 1, -1, -1):
            h = self._msg_heights[i]
            y -= h
            if y + h <= start_y:
                break          # 完全在视口上方，更旧的消息也无需处理
            if y >= bottom_y:
                continue       # 完全在视口下方（被滚出底部）
            username, content = self._messages[i]
            text = f"{username}：{content}"
            tc.draw_wrapped(text, padding_x, y, text_width,
                           "微软雅黑", 10, False, 255, 255, 255)

        return True
