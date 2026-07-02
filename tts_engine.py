"""
TTS 引擎：Edge TTS 语音合成 + 音频播放。
"""

import audioop
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from io import BytesIO

import simpleaudio as sa

try:
    import edge_tts
    from edge_tts.constants import DEFAULT_VOICE
except ImportError:
    edge_tts = None
    DEFAULT_VOICE = "zh-CN-XiaoyiNeural"

import requests


class TtsClient:
    """Edge TTS 语音合成客户端。"""

    def __init__(self, voice):
        self.voice = voice.strip() or DEFAULT_VOICE

    def synthesize(self, text):
        if edge_tts is None:
            raise RuntimeError("edge-tts 未安装，请运行 pip install edge-tts")

        communicate = edge_tts.Communicate(text, self.voice)
        audio_bytes = bytearray()
        for chunk in communicate.stream_sync():
            if chunk["type"] == "audio":
                audio_bytes.extend(chunk["data"])

        if not audio_bytes:
            raise RuntimeError("edge-tts 未返回音频数据")
        return bytes(audio_bytes)


class GptSovitsClient:
    """GPT-SoVITS 语音合成客户端。"""

    def __init__(self, api_url, ref_audio_path="", prompt_text="",
                 prompt_lang="ja", text_lang="zh"):
        self.api_url = api_url.rstrip('/')
        self.ref_audio_path = ref_audio_path
        self.prompt_text = prompt_text
        self.prompt_lang = prompt_lang
        self.text_lang = text_lang

    def synthesize(self, text):
        """调用 GPT-SoVITS API 将文本转为语音，返回 WAV 音频字节。"""
        payload = {
            "text_split_method": "cut0", # 不切
            "text": text,
            "text_lang": self.text_lang,
            "ref_audio_path": self.ref_audio_path,
            "prompt_text": self.prompt_text,
            "prompt_lang": self.prompt_lang,
            "streaming_mode": False,
        }
        try:
            resp = requests.post(
                f"{self.api_url}/tts",
                json=payload,
                timeout=60,
            )
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                f"无法连接 GPT-SoVITS 服务 ({self.api_url})，请确认服务已启动")
        except requests.exceptions.Timeout:
            raise RuntimeError("GPT-SoVITS 请求超时")
        except requests.exceptions.RequestException as ex:
            raise RuntimeError(f"GPT-SoVITS 网络请求失败: {ex}")

        if resp.status_code == 200:
            return resp.content
        else:
            try:
                err = resp.json()
            except Exception:
                err = resp.text
            raise RuntimeError(f"GPT-SoVITS 请求失败 (HTTP {resp.status_code}): {err}")


class AudioPlayer:
    """单线程音频播放器，使用队列管理播放请求。"""

    def __init__(self, on_log):
        self._on_log = on_log
        self._volume = 100
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._thread.start()

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value):
        self._volume = max(0, min(100, int(value)))

    def enqueue(self, audio_bytes):
        """将音频数据入队，由播放线程异步播放。"""
        try:
            self._queue.put(audio_bytes)
        except Exception as ex:
            self._on_log(f"音频入队失败: {ex}")

    def clear(self):
        """清空播放队列。"""
        try:
            while True:
                self._queue.get_nowait()
                try:
                    self._queue.task_done()
                except Exception:
                    pass
        except queue.Empty:
            pass

    def _playback_loop(self):
        while True:
            try:
                audio_bytes = self._queue.get()
            except Exception as ex:
                self._on_log(f"播放线程获取队列失败: {ex}")
                time.sleep(0.1)
                continue

            try:
                self._play_audio(audio_bytes)
            except Exception as ex:
                self._on_log(f"播放线程播放失败: {ex}")
            finally:
                try:
                    self._queue.task_done()
                except Exception:
                    pass

    def _play_audio(self, audio_bytes):
        if audio_bytes.startswith(b"RIFF"):
            self._play_wav(audio_bytes)
        else:
            self._play_mp3(audio_bytes)

    def _play_wav(self, audio_bytes):
        """使用 ffplay 播放 WAV 音频，避免 simpleaudio 在某些 WAV 格式下崩溃。"""
        if os.name != "nt":
            raise RuntimeError("本程序仅支持 Windows 平台。")

        local_ffplay = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ffplay.exe"
        )
        ffplay_cmd = (
            local_ffplay if os.path.exists(local_ffplay) else shutil.which("ffplay")
        )
        if ffplay_cmd is None:
            # 回退到 simpleaudio
            self._play_wav_simpleaudio(audio_bytes)
            return

        wav_file = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_wav:
                tmp_wav.write(audio_bytes)
                wav_file = tmp_wav.name

            cmd = [
                ffplay_cmd,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-volume",
                str(self._volume),
                "-i",
                wav_file,
            ]
            subprocess.run(
                cmd, check=True, creationflags=subprocess.CREATE_NO_WINDOW
            )
        except subprocess.CalledProcessError as ex:
            raise RuntimeError(f"ffplay 播放失败: {ex}")
        except Exception as ex:
            raise RuntimeError(f"WAV 播放失败: {ex}")
        finally:
            if wav_file:
                try:
                    os.remove(wav_file)
                except OSError:
                    pass

    def _play_wav_simpleaudio(self, audio_bytes):
        """回退方案：使用 simpleaudio 播放 WAV。"""
        with BytesIO(audio_bytes) as buffer:
            try:
                with wave.open(buffer, "rb") as wf:
                    frames = wf.readframes(wf.getnframes())
                    if self._volume != 100:
                        frames = audioop.mul(
                            frames, wf.getsampwidth(), self._volume / 100.0
                        )
                    wave_obj = sa.WaveObject(
                        frames, wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
                    )
                    play_obj = wave_obj.play()
                    play_obj.wait_done()
            except Exception as ex:
                raise RuntimeError(f"音频播放失败: {ex}")

    def _play_mp3(self, mp3_bytes):
        if os.name != "nt":
            raise RuntimeError("本程序仅支持 Windows 平台。")

        local_ffplay = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ffplay.exe"
        )
        ffplay_cmd = (
            local_ffplay if os.path.exists(local_ffplay) else shutil.which("ffplay")
        )
        if ffplay_cmd is None:
            raise RuntimeError(
                "未找到 ffplay。请将 ffplay.exe 放在程序目录或添加到 PATH。"
            )

        mp3_file = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_mp3:
                tmp_mp3.write(mp3_bytes)
                mp3_file = tmp_mp3.name

            cmd = [
                ffplay_cmd,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-volume",
                str(self._volume),
                "-i",
                mp3_file,
            ]
            subprocess.run(
                cmd, check=True, creationflags=subprocess.CREATE_NO_WINDOW
            )
        except subprocess.CalledProcessError as ex:
            raise RuntimeError(f"ffplay 播放失败: {ex}")
        except Exception as ex:
            raise RuntimeError(f"MP3 播放失败: {ex}")
        finally:
            if mp3_file:
                try:
                    os.remove(mp3_file)
                except OSError:
                    pass
