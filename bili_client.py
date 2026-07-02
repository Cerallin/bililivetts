"""
Bilibili 弹幕客户端：自实现协议客户端 + blivedm 封装客户端。
"""

import json
import struct
import threading
import time
import zlib
import asyncio

import requests
import websocket

try:
    import brotli
except ImportError:
    brotli = None

try:
    import blivedm
    import blivedm.models.web as web_models
except Exception:
    blivedm = None

try:
    import aiohttp
except Exception:
    aiohttp = None


class BiliDanmakuClient(threading.Thread):
    """基于原始 WebSocket 协议的弹幕客户端（不依赖 blivedm）。"""

    def __init__(self, room_id, watch_uid, sessdata, on_danmaku, on_log, on_disconnect):
        super().__init__(daemon=True)
        self.room_id = int(room_id)
        self.watch_uid = str(watch_uid).strip()
        self.sessdata = str(sessdata).strip()
        self.on_danmaku = on_danmaku
        self.on_log = on_log
        self.on_disconnect = on_disconnect
        self._stop_event = threading.Event()
        self.ws = None
        self.session = None
        self._uid = 0
        self._host_server_token = None
        self._ws_host = None
        self._ws_port = None

    def stop(self):
        self._stop_event.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def _log(self, message):
        self.on_log(message)

    def run(self):
        try:
            self._log(f"正在初始化直播间 {self.room_id} ...")
            self._init_session()
            room_id = self._resolve_room_id(self.room_id)
            self._log(f"实际直播房间ID：{room_id}")
            self.room_id = room_id
            self._uid = self._get_uid()
            self._init_danmaku_server_info(room_id)
            self._connect_danmaku(room_id)
        except Exception as ex:
            self._log(f"弹幕连接失败: {ex}")
            self.on_disconnect()

    def _resolve_room_id(self, room_id):
        url = "https://api.live.bilibili.com/room/v1/Room/room_init"
        resp = self.session.get(url, params={"id": room_id}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(data.get("message", "无法获取直播间信息"))
        return int(data["data"]["room_id"])

    def _init_session(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            "Referer": "https://live.bilibili.com/",
            "Origin": "https://live.bilibili.com",
        })
        if self.sessdata:
            self.session.cookies.set(
                "SESSDATA", self.sessdata, domain="bilibili.com", path="/")

    def _get_uid(self):
        if not self.sessdata:
            return 0
        try:
            url = "https://api.bilibili.com/x/web-interface/nav"
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                return 0
            info = data.get("data", {})
            if info.get("isLogin"):
                return int(info.get("mid", 0))
        except Exception:
            pass
        return 0

    def _init_danmaku_server_info(self, room_id):
        try:
            url = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
            resp = self.session.get(
                url, params={"id": room_id, "type": 0}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0:
                host_list = data["data"].get("host_list", [])
                if host_list:
                    first_host = host_list[0]
                    self._ws_host = first_host.get("host")
                    self._ws_port = first_host.get(
                        "wss_port") or first_host.get("ws_port")
                    self._host_server_token = data["data"].get("token")
                    return
        except Exception:
            pass
        self._ws_host = "broadcastlv.chat.bilibili.com"
        self._ws_port = 443
        self._host_server_token = None

    def _connect_danmaku(self, room_id):
        protocol = "wss"
        if self._ws_port == 2244:
            protocol = "ws"
        ws_url = f"{protocol}://{self._ws_host}:{self._ws_port}/sub"
        headers = [
            "Origin: https://live.bilibili.com",
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        self.ws = websocket.WebSocketApp(
            ws_url,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._log("开始连接弹幕 WebSocket...")
        self.ws.run_forever()

    def _on_open(self, ws):
        self._log("WebSocket 已连接，发送鉴权请求...")
        auth_data = json.dumps({
            "uid": self._uid,
            "roomid": self.room_id,
            "protover": 3,
            "platform": "web",
            "type": 2,
            "key": self._host_server_token or "",
        }).encode("utf-8")
        ws.send(self._pack_packet(auth_data, op=7))
        heartbeat = self._pack_packet(b"", op=2)
        ws.send(heartbeat)
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    def _heartbeat_loop(self):
        while not self._stop_event.is_set():
            time.sleep(30)
            if self.ws:
                try:
                    self.ws.send(self._pack_packet(b"", op=2))
                except Exception as ex:
                    self._log(f"心跳发送失败: {ex}")
                    break

    def _on_message(self, ws, message):
        if isinstance(message, str):
            message = message.encode("utf-8")
        self._parse_packet(message)

    def _on_error(self, ws, error):
        self._log(f"WebSocket 错误: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        self._log("WebSocket 已关闭")
        self.on_disconnect()

    def _pack_packet(self, body, op=7):
        packet_len = 16 + len(body)
        return struct.pack(
            ">IHHII",
            packet_len,
            16,
            1,
            op,
            1,
        ) + body

    def _parse_packet(self, data):
        offset = 0
        while offset + 16 <= len(data):
            packet_len, header_len, ver, op, seq = struct.unpack(
                ">IHHII", data[offset: offset + 16]
            )
            body = data[offset + header_len: offset + packet_len]
            if op == 5:
                if ver == 2:
                    try:
                        body = zlib.decompress(body)
                    except Exception as ex:
                        self._log(f"zlib 解包失败: {ex}")
                        return
                    self._parse_packet(body)
                elif ver == 3 and brotli is not None:
                    try:
                        body = brotli.decompress(body)
                    except Exception as ex:
                        self._log(f"brotli 解包失败: {ex}")
                        return
                    self._parse_packet(body)
                else:
                    self._handle_json_payload(body)
            elif op == 3:
                try:
                    count = struct.unpack(">I", body[:4])[0]
                    self._log(f"当前房间在线人数：{count}")
                except Exception:
                    pass
            elif op == 8:
                self._log("鉴权成功，已加入房间弹幕服务")
            offset += packet_len

    def _handle_json_payload(self, payload):
        try:
            text = payload.decode("utf-8", errors="ignore")
            obj = json.loads(text)
        except Exception:
            return
        if isinstance(obj, dict) and obj.get("cmd") == "DANMU_MSG":
            info = obj.get("info", [])
            if len(info) >= 3:
                content = info[1]
                uid = str(info[2][0])
                uname = str(info[2][1]) if len(info[2]) > 1 else uid
                if self.watch_uid == "" or uid == self.watch_uid:
                    self.on_danmaku(uid, uname, content)
        elif isinstance(obj, dict) and obj.get("cmd") == "LIVE":
            self._log("直播间状态变化：" + str(obj.get("msg", "")))


class BlivedmDanmakuClient(threading.Thread):
    """基于 xfgryujk/blivedm 的弹幕客户端。"""

    def __init__(self, room_id, watch_uid, sessdata, on_danmaku, on_log, on_disconnect):
        super().__init__(daemon=True)
        self.room_id = int(room_id)
        self.watch_uid = str(watch_uid).strip()
        self.sessdata = str(sessdata).strip()
        self.on_danmaku = on_danmaku
        self.on_log = on_log
        self.on_disconnect = on_disconnect
        self._stop_event = threading.Event()
        self._client = None
        self._session = None

    def stop(self):
        self._stop_event.set()

    def _log(self, msg):
        self.on_log(msg)

    def run(self):
        if blivedm is None or aiohttp is None:
            self._log("blivedm 或 aiohttp 未安装，无法使用 blivedm 客户端")
            self.on_disconnect()
            return
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._run_async())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    async def _run_async(self):
        try:
            cookies = {}
            if self.sessdata:
                cookies['SESSDATA'] = self.sessdata
            self._session = aiohttp.ClientSession()
            if cookies:
                try:
                    self._session.cookie_jar.update_cookies(cookies)
                except Exception:
                    pass

            client = blivedm.BLiveClient(self.room_id, session=self._session)

            class Handler(blivedm.BaseHandler):
                def __init__(self, outer):
                    super().__init__()
                    self._outer = outer

                def handle(self, client_obj, command: dict):
                    cmd = command.get('cmd', '') if isinstance(command, dict) else ''
                    if cmd in ('DANMU_MSG', '_HEARTBEAT', 'LIVE'):
                        try:
                            return super().handle(client_obj, command)
                        except Exception:
                            return
                    return

                def _on_danmaku(self, client_obj, message: web_models.DanmakuMessage):
                    try:
                        uid = getattr(message, 'uid', None) or getattr(
                            message, 'mid', None) or getattr(message, 'user_id', None) or 0
                        msg = getattr(message, 'msg', None) or getattr(
                            message, 'message', '')
                        uname = getattr(message, 'uname', None) or ''
                        uid_str = str(uid)
                        if self._outer.watch_uid == "" or uid_str == self._outer.watch_uid:
                            self._outer.on_danmaku(uid_str, uname, msg)
                    except Exception:
                        pass

                def _on_heartbeat(self, client_obj, message):
                    self._outer.on_log(f"[{self._outer.room_id}] 心跳")

            handler = Handler(self)
            client.set_handler(handler)
            self._client = client

            client.start()
            self._log(f"使用 blivedm 连接房间 {self.room_id}")

            while not self._stop_event.is_set():
                await asyncio.sleep(1)

            client.stop()
            await client.join()
            await client.stop_and_close()
        except Exception as ex:
            self._log(f"blivedm 客户端异常: {ex}")
        finally:
            try:
                if self._session is not None:
                    await self._session.close()
            except Exception:
                pass
            self.on_disconnect()


def _resolve_room_id(room_id, session, log_func=None):
    """将直播间短号解析为真实 room_id。

    Args:
        room_id: 直播间号（可能是短号）。
        session: requests.Session 实例。
        log_func: 可选日志回调。

    Returns:
        int 或 None: 真实 room_id，解析失败返回 None。
    """
    try:
        url = "https://api.live.bilibili.com/room/v1/Room/room_init"
        resp = session.get(url, params={"id": room_id}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0:
            real_id = int(data["data"]["room_id"])
            if log_func:
                log_func(f"直播间 {room_id} 解析为真实 ID: {real_id}")
            return real_id
    except Exception as ex:
        if log_func:
            log_func(f"解析直播间 ID 失败: {ex}")
    return None


def send_danmaku_to_room(room_id, message, sessdata, on_log=None, bili_jct=None):
    """通过 Bilibili HTTP API 向直播间发送弹幕。

    需要有效的 SESSDATA（登录态）才能发送。CSRF token 优先使用
    传入的 bili_jct 参数，若未提供则尝试从 cookie 中自动获取。

    Args:
        room_id: 直播间号（短号或长号均可，API 会自动解析）。
        message: 弹幕文本内容。
        sessdata: B 站 SESSDATA cookie 值。
        on_log: 可选日志回调，签名为 on_log(message: str)。
        bili_jct: 可选 CSRF token，即浏览器 cookie 中的 bili_jct 值。
                  若提供则直接使用，否则自动尝试获取。

    Returns:
        bool: 发送成功返回 True，否则返回 False。
    """

    def _log(msg):
        if on_log:
            on_log(msg)

    if not sessdata:
        _log("未设置 SESSDATA，无法发送弹幕（需要登录）")
        return False

    if not message or not message.strip():
        _log("弹幕内容为空，跳过发送")
        return False

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "Referer": "https://live.bilibili.com/",
        "Origin": "https://live.bilibili.com",
    })
    session.cookies.set(
        "SESSDATA", sessdata, domain="bilibili.com", path="/")

    # 若用户未提供 bili_jct，尝试自动获取
    if not bili_jct:
        _log("未填写 bili_jct，尝试自动获取...")
        try:
            session.get(
                "https://api.bilibili.com/x/web-interface/nav", timeout=10)
            bili_jct = session.cookies.get("bili_jct", domain="bilibili.com")
            if not bili_jct:
                bili_jct = session.cookies.get("bili_jct")
        except Exception as ex:
            _log(f"自动获取 CSRF token 失败: {ex}")

    if not bili_jct:
        _log(
            "无法获取 CSRF token (bili_jct)，请在主窗口填写 bili_jct 字段。\n"
            "获取方式：浏览器打开 bilibili.com → F12 → Application → "
            "Cookies → 复制 bili_jct 的值"
        )
        return False

    # 关键：bili_jct 必须同时存在于 Cookie 和 POST body 中
    session.cookies.set(
        "bili_jct", bili_jct, domain="bilibili.com", path="/")

    # 解析直播间短号为真实 room_id
    try:
        room_id = int(room_id)
    except (ValueError, TypeError):
        _log(f"无效的直播间号: {room_id}")
        return False
    real_room_id = _resolve_room_id(room_id, session, _log)
    if real_room_id is None:
        _log(f"无法解析直播间 {room_id}，使用原始 ID 尝试发送")
        real_room_id = room_id

    # 更新 Referer 为具体直播间页面
    session.headers["Referer"] = f"https://live.bilibili.com/{real_room_id}"

    # 发送弹幕 — 注意 roomid 必须在 POST body 中
    data = {
        "bubble": "0",
        "msg": message,
        "color": "16777215",       # 白色
        "mode": "1",               # 滚动弹幕
        "fontsize": "25",
        "rnd": str(int(time.time())),  # 秒级时间戳，非毫秒
        "roomid": str(real_room_id),   # 真实房间 ID（必须）
        "csrf": bili_jct,
        "csrf_token": bili_jct,
    }

    try:
        resp = session.post(
            "https://api.live.bilibili.com/msg/send",
            data=data,
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == 0:
            _log(f"弹幕发送成功: {message}")
            return True
        else:
            msg = result.get("message", "未知错误")
            code = result.get("code", -1)
            _log(f"弹幕发送失败: {msg} (code={code})")
            return False
    except Exception as ex:
        _log(f"弹幕发送异常: {ex}")
        return False
