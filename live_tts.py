"""
直播弹幕 TTS —— 主程序入口。
从 Bilibili 直播间获取弹幕，对目标用户的弹幕文字转语音实时播放。
"""

import json
import os
import shutil
import sys
import threading
import time

import wx

from bili_client import BiliDanmakuClient, BlivedmDanmakuClient, send_danmaku_to_room
from tts_engine import TtsClient, AudioPlayer
from ui_popup import PopupTtsFrame, DanmakuInteractionFrame

LOG_FORMAT = "%Y-%m-%d %H:%M:%S"


class LiveTtsFrame(wx.Frame):
    """主窗口：直播间配置、TTS 控制、日志。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, title="直播弹幕 TTS", size=(720, 520))
        self.danmaku_client = None
        self._popup_frame = None          # 旧的简单弹窗
        self._interaction_frame = None    # 新的直播互动弹窗
        self._popup_opacity = 230
        self._volume = 100
        self._interaction_win_width = 400
        self._interaction_win_height = 520

        # 音频播放器
        self._audio_player = AudioPlayer(on_log=self._log)

        self._build_ui()
        try:
            self._load_config()
        except Exception:
            pass
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(7, 2, 10, 10)
        grid.AddGrowableCol(1, 1)

        grid.Add(
            wx.StaticText(panel, label="[必填] 直播间号："), 0, wx.ALIGN_CENTER_VERTICAL
        )
        self.room_input = wx.TextCtrl(panel)
        grid.Add(self.room_input, 1, wx.EXPAND)

        grid.Add(
            wx.StaticText(panel, label="[选填] 目标用户 UID："), 0, wx.ALIGN_CENTER_VERTICAL
        )
        self.uid_input = wx.TextCtrl(panel)
        grid.Add(self.uid_input, 1, wx.EXPAND)

        grid.Add(
            wx.StaticText(panel, label="[解弹幕用] SESSDATA："), 0, wx.ALIGN_CENTER_VERTICAL
        )
        self.sessdata_input = wx.TextCtrl(panel, style=wx.TE_PASSWORD)
        grid.Add(self.sessdata_input, 1, wx.EXPAND)

        grid.Add(
            wx.StaticText(panel, label="[发弹幕用] bili_jct："), 0, wx.ALIGN_CENTER_VERTICAL
        )
        self.bili_jct_input = wx.TextCtrl(panel, style=wx.TE_PASSWORD)
        grid.Add(self.bili_jct_input, 1, wx.EXPAND)

        grid.Add(
            wx.StaticText(panel, label="TTS 语音："), 0, wx.ALIGN_CENTER_VERTICAL
        )
        self.voice_choice = wx.Choice(
            panel,
            choices=[
                "zh-CN-XiaoxiaoNeural",
                "zh-CN-XiaoyiNeural",
                "zh-CN-liaoning-XiaobeiNeural",
                "zh-CN-shaanxi-XiaoniNeural",
            ],
        )
        self.voice_choice.SetSelection(0)
        grid.Add(self.voice_choice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="说明："), 0, wx.ALIGN_TOP)
        self.hint_text = wx.StaticText(
            panel,
            label=(
                "填写目标用户UID时将只播报该用户的弹幕，留空则不播报所有弹幕。\n"
                "SESSDATA和bili_jct分别用于解析/发送弹幕，可从浏览器F12 → Application → Cookies中获取。"
            ),
        )
        self.hint_text.Wrap(460)
        grid.Add(self.hint_text, 1, wx.EXPAND)

        main_sizer.Add(grid, 0, wx.ALL | wx.EXPAND, 12)

        # 按钮行
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.start_button = wx.Button(panel, label="启动监听")
        self.stop_button = wx.Button(panel, label="停止监听")
        self.test_button = wx.Button(panel, label="TTS 测试")
        self.interaction_button = wx.Button(panel, label="打开直播互动窗口")
        self.stop_button.Disable()
        btn_sizer.Add(self.start_button, 0, wx.RIGHT, 10)
        btn_sizer.Add(self.stop_button, 0, wx.RIGHT, 10)
        btn_sizer.Add(self.test_button, 0, wx.RIGHT, 10)
        btn_sizer.Add(self.interaction_button, 0, wx.RIGHT, 10)
        main_sizer.Add(btn_sizer, 0, wx.ALL, 12)

        # 透明度滑块
        opacity_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.opacity_label = wx.StaticText(panel, label="直播互动窗口透明度：90%")
        self.opacity_slider = wx.Slider(
            panel, value=230, minValue=5, maxValue=255, style=wx.SL_HORIZONTAL
        )
        opacity_sizer.Add(
            self.opacity_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10
        )
        opacity_sizer.Add(self.opacity_slider, 1, wx.EXPAND)
        main_sizer.Add(opacity_sizer, 0, wx.ALL | wx.EXPAND, 12)

        # 音量滑块
        volume_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.volume_label = wx.StaticText(panel, label="TTS 音量：100%")
        self.volume_slider = wx.Slider(
            panel, value=100, minValue=0, maxValue=100, style=wx.SL_HORIZONTAL
        )
        volume_sizer.Add(
            self.volume_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10
        )
        volume_sizer.Add(self.volume_slider, 1, wx.EXPAND)
        main_sizer.Add(volume_sizer, 0, wx.ALL | wx.EXPAND, 12)

        # 日志区
        self.log_ctrl = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL
        )
        main_sizer.Add(self.log_ctrl, 1, wx.ALL | wx.EXPAND, 12)

        panel.SetSizer(main_sizer)

        # 事件绑定
        self.start_button.Bind(wx.EVT_BUTTON, self.on_start)
        self.stop_button.Bind(wx.EVT_BUTTON, self.on_stop)
        self.test_button.Bind(wx.EVT_BUTTON, self.on_test_tts)
        self.interaction_button.Bind(wx.EVT_BUTTON, self.on_open_interaction)
        self.opacity_slider.Bind(wx.EVT_SLIDER, self.on_opacity_change)
        self.volume_slider.Bind(wx.EVT_SLIDER, self.on_volume_change)

        # 自动保存配置
        self.room_input.Bind(wx.EVT_TEXT, self._on_field_change)
        self.uid_input.Bind(wx.EVT_TEXT, self._on_field_change)
        self.sessdata_input.Bind(wx.EVT_TEXT, self._on_field_change)
        self.bili_jct_input.Bind(wx.EVT_TEXT, self._on_field_change)
        self.voice_choice.Bind(wx.EVT_CHOICE, self._on_voice_change)

    # ------------------------------------------------------------------
    # 弹幕监听
    # ------------------------------------------------------------------

    def on_start(self, event):
        room_id = self.room_input.GetValue().strip()
        watch_uid = self.uid_input.GetValue().strip()
        voice = self.voice_choice.GetStringSelection()
        if not room_id:
            self._log("请填写直播间号")
            return

        self.start_button.Disable()
        self.stop_button.Enable()
        self._log("启动弹幕监听...")

        sessdata_val = self.sessdata_input.GetValue().strip()
        if blivedm_available():
            self.danmaku_client = BlivedmDanmakuClient(
                room_id,
                watch_uid,
                sessdata_val,
                on_danmaku=self._on_danmaku,
                on_log=self._log,
                on_disconnect=self._on_disconnect,
            )
        else:
            self.danmaku_client = BiliDanmakuClient(
                room_id,
                watch_uid,
                sessdata_val,
                on_danmaku=self._on_danmaku,
                on_log=self._log,
                on_disconnect=self._on_disconnect,
            )
        self._current_tts_voice = voice
        self.danmaku_client.start()

        # 如果互动窗口已打开，清空旧消息
        if self._interaction_frame is not None:
            try:
                self._interaction_frame.clear_messages()
            except Exception:
                pass

    def on_stop(self, event):
        self._stop_listening()

    def on_test_tts(self, event):
        voice = self.voice_choice.GetStringSelection()
        self._log("开始 TTS 测试...")
        self._play_text("测试。麦克风测试。", voice)

    # ------------------------------------------------------------------
    # 弹幕回调
    # ------------------------------------------------------------------

    def _on_danmaku(self, uid, uname, content):
        self._log(f"过滤到目标弹幕 UID={uid}，文本：{content}")

        # 当目标用户 UID 为空或为 "0" 时，不播报 TTS
        watch_uid = self.uid_input.GetValue().strip()
        if watch_uid and watch_uid != "0":
            self._play_text(content, self.voice_choice.GetStringSelection())

        # 转发到互动窗口
        if self._interaction_frame is not None:
            try:
                wx.CallAfter(self._interaction_frame.add_danmaku, uid, uname, content)
            except Exception:
                pass

    def _play_text(self, text, voice):
        def run():
            try:
                tts = TtsClient(voice)
                audio_bytes = tts.synthesize(text)
                self._audio_player.enqueue(audio_bytes)
            except Exception as ex:
                self._log(f"TTS 播放失败: {ex}")

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------
    # 监听控制
    # ------------------------------------------------------------------

    def _stop_listening(self):
        if self.danmaku_client:
            self.danmaku_client.stop()
            self.danmaku_client = None
            self._log("已停止弹幕监听")
        self._audio_player.clear()
        self.start_button.Enable()
        self.stop_button.Disable()

    def _on_disconnect(self):
        wx.CallAfter(self._stop_listening)

    # ------------------------------------------------------------------
    # 直播互动窗口
    # ------------------------------------------------------------------

    def _ensure_interaction_frame(self):
        if self._interaction_frame is None:
            self._interaction_frame = DanmakuInteractionFrame(
                self,
                on_send=self._on_interaction_send,
                on_close_cb=self._on_interaction_close,
                initial_width=self._interaction_win_width,
                initial_height=self._interaction_win_height,
            )
            self._interaction_frame.set_bg_opacity(self._popup_opacity)
        return self._interaction_frame

    def on_open_interaction(self, event):
        frame = self._ensure_interaction_frame()
        if not frame.IsShown():
            frame.Show()
        frame.set_bg_opacity(self._popup_opacity)
        frame.Raise()

    def _on_interaction_send(self, text):
        if not text:
            return
        self._log(f"互动窗口输入: {text}")
        # TTS 语音播报（无论是否监听中都播放）
        self._play_text(text, self.voice_choice.GetStringSelection())
        # 仅在监听运行中时才同步发布弹幕到直播间
        if self.danmaku_client is None:
            self._log("未启动监听，仅 TTS 播报，不发送弹幕")
            return
        room_id = self.room_input.GetValue().strip()
        sessdata = self.sessdata_input.GetValue().strip()
        bili_jct = self.bili_jct_input.GetValue().strip()
        if room_id and sessdata:
            threading.Thread(
                target=lambda: send_danmaku_to_room(
                    room_id, text, sessdata, self._log, bili_jct=bili_jct),
                daemon=True,
            ).start()
        else:
            self._log("未设置直播间号或 SESSDATA，无法同步发送弹幕")

    def _on_interaction_close(self):
        if self._interaction_frame is not None:
            try:
                w, h = self._interaction_frame.get_window_size()
                self._interaction_win_width = w
                self._interaction_win_height = h
            except Exception:
                pass
        self._interaction_frame = None
        self._save_config()

    # ------------------------------------------------------------------
    # 透明度 & 音量
    # ------------------------------------------------------------------

    def on_opacity_change(self, event):
        self._popup_opacity = event.GetInt()
        pct = round(self._popup_opacity / 255 * 100)
        self.opacity_label.SetLabel(f"直播互动窗口透明度：{pct}%")
        if self._interaction_frame is not None:
            self._interaction_frame.set_bg_opacity(self._popup_opacity)
        self._save_config()

    def on_volume_change(self, event):
        self._volume = event.GetInt()
        self.volume_label.SetLabel(f"TTS 音量：{self._volume}%")
        self._audio_player.volume = self._volume
        self._save_config()

    # ------------------------------------------------------------------
    # 配置持久化
    # ------------------------------------------------------------------

    def _config_file_path(self):
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "config.json")

    def _load_config(self):
        path = self._config_file_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception as ex:
            self._log(f"读取配置失败: {ex}")
            return

        room = cfg.get('room', '')
        watch_uid = cfg.get('watch_uid', '')
        sessdata = cfg.get('sessdata', '')
        bili_jct = cfg.get('bili_jct', '')
        voice = cfg.get('voice', '')
        opacity = cfg.get('popup_opacity', 230)
        volume = cfg.get('volume', 100)
        self._interaction_win_width = int(cfg.get('interaction_win_width', 400))
        self._interaction_win_height = int(cfg.get('interaction_win_height', 520))

        try:
            self.room_input.SetValue(room)
            self.uid_input.SetValue(watch_uid)
            self.sessdata_input.SetValue(sessdata)
            self.bili_jct_input.SetValue(bili_jct)
            if voice:
                try:
                    idx = self.voice_choice.GetItems().index(voice)
                    self.voice_choice.SetSelection(idx)
                except ValueError:
                    pass
            self._popup_opacity = int(opacity)
            self.opacity_slider.SetValue(self._popup_opacity)
            self.opacity_label.SetLabel(
                f"直播互动窗口透明度：{round(self._popup_opacity / 255 * 100)}%"
            )
            self._volume = int(volume)
            self.volume_slider.SetValue(self._volume)
            self.volume_label.SetLabel(f"TTS 音量：{self._volume}%")
            self._audio_player.volume = self._volume
        except Exception:
            pass

    def _save_config(self):
        path = self._config_file_path()
        cfg = {
            'room': self.room_input.GetValue().strip(),
            'watch_uid': self.uid_input.GetValue().strip(),
            'sessdata': self.sessdata_input.GetValue().strip(),
            'bili_jct': self.bili_jct_input.GetValue().strip(),
            'voice': self.voice_choice.GetStringSelection(),
            'popup_opacity': self._popup_opacity,
            'volume': self._volume,
            'interaction_win_width': self._interaction_win_width,
            'interaction_win_height': self._interaction_win_height,
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            try:
                os.replace(tmp, path)
            except Exception:
                shutil.move(tmp, path)
        except Exception as ex:
            try:
                self._log(f"保存配置失败: {ex}")
            except Exception:
                pass

    def _on_field_change(self, event):
        self._save_config()
        event.Skip()

    def _on_voice_change(self, event):
        self._save_config()
        event.Skip()

    def _on_close(self, event):
        # 关闭子窗口（必须用 Close() 而非 Destroy()，否则 EVT_CLOSE 不会触发，
        # 导致子窗口内部的清理逻辑被跳过，造成资源泄漏例如独立输入框残留）
        if self._interaction_frame is not None:
            try:
                self._interaction_frame.Close()
            except Exception:
                pass
        if self._popup_frame is not None:
            try:
                self._popup_frame.Close()
            except Exception:
                pass
        self._stop_listening()
        self._save_config()
        self.Destroy()

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def _log(self, message):
        timestamp = time.strftime(LOG_FORMAT)
        try:
            wx.CallAfter(self.log_ctrl.AppendText, f"[{timestamp}] {message}\n")
        except Exception:
            pass


# ------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------

def blivedm_available():
    """检查 blivedm 和 aiohttp 是否可用。"""
    try:
        import blivedm  # noqa: F401
        import aiohttp  # noqa: F401
        return True
    except Exception:
        return False


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------

def main():
    app = wx.App(False)
    frame = LiveTtsFrame(None)
    frame.Show()
    app.MainLoop()


if __name__ == "__main__":
    main()

