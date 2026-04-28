"""
Remote Control Client — запускается на управляемом ПК.
Подключается к серверу и выполняет команды.
"""

import socket
import threading
import struct
import json
import time
import io
import os
import sys
import base64
import subprocess
import traceback

# Опциональные зависимости
try:
    import pyautogui
    PYAUTOGUI_OK = True
except ImportError:
    PYAUTOGUI_OK = False

try:
    import mss
    MSS_OK = True
except ImportError:
    MSS_OK = False

try:
    import cv2
    CV2_OK = True
except ImportError:
    CV2_OK = False

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    import pynput.keyboard as pynput_kb
    import pynput.mouse as pynput_ms
    PYNPUT_OK = True
except ImportError:
    PYNPUT_OK = False

SERVER_HOST = "127.0.0.1"   # <- поменяй на IP сервера
SERVER_PORT = 9999
BUFFER_SIZE  = 4096
SCREEN_FPS   = 15            # кадров в секунду для стриминга экрана
CAMERA_FPS   = 10


# ─────────────────────────── утилиты ────────────────────────────

def send_packet(sock: socket.socket, data: bytes):
    """Отправляет пакет: 4 байта длины + payload."""
    length = struct.pack(">I", len(data))
    sock.sendall(length + data)


def recv_packet(sock: socket.socket) -> bytes:
    """Читает ровно один пакет."""
    raw_len = _recv_exact(sock, 4)
    if not raw_len:
        return b""
    length = struct.unpack(">I", raw_len)[0]
    return _recv_exact(sock, length)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return b""
        buf += chunk
    return buf


def send_json(sock: socket.socket, obj: dict):
    send_packet(sock, json.dumps(obj).encode())


def recv_json(sock: socket.socket) -> dict:
    raw = recv_packet(sock)
    if not raw:
        return {}
    return json.loads(raw.decode())


# ─────────────────────────── обработчики команд ─────────────────

def handle_screenshot(sock: socket.socket, _cmd: dict):
    """Делает скриншот и отправляет JPEG."""
    if MSS_OK:
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            img = sct.grab(monitor)
            if PIL_OK:
                pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
                buf = io.BytesIO()
                pil.save(buf, format="JPEG", quality=60)
                data = buf.getvalue()
            else:
                data = b""
    elif PIL_OK:
        import PIL.ImageGrab
        img = PIL.ImageGrab.grab()
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        data = buf.getvalue()
    else:
        data = b""

    send_json(sock, {"type": "screenshot", "size": len(data)})
    if data:
        send_packet(sock, data)


def handle_mouse(sock: socket.socket, cmd: dict):
    """Выполняет действие мышью."""
    if not PYAUTOGUI_OK:
        send_json(sock, {"type": "error", "msg": "pyautogui not installed"})
        return
    action = cmd.get("action")
    x = cmd.get("x", 0)
    y = cmd.get("y", 0)
    button = cmd.get("button", "left")
    try:
        if action == "move":
            pyautogui.moveTo(x, y, duration=0.05)
        elif action == "click":
            pyautogui.click(x, y, button=button)
        elif action == "double_click":
            pyautogui.doubleClick(x, y)
        elif action == "right_click":
            pyautogui.rightClick(x, y)
        elif action == "scroll":
            pyautogui.scroll(cmd.get("amount", 3), x=x, y=y)
        elif action == "drag":
            x2, y2 = cmd.get("x2", x), cmd.get("y2", y)
            pyautogui.dragTo(x2, y2, duration=0.3)
        send_json(sock, {"type": "ok"})
    except Exception as e:
        send_json(sock, {"type": "error", "msg": str(e)})


def handle_keyboard(sock: socket.socket, cmd: dict):
    """Нажатие клавиш / ввод текста."""
    if not PYAUTOGUI_OK:
        send_json(sock, {"type": "error", "msg": "pyautogui not installed"})
        return
    action = cmd.get("action")
    try:
        if action == "type":
            pyautogui.typewrite(cmd.get("text", ""), interval=0.03)
        elif action == "hotkey":
            keys = cmd.get("keys", [])
            pyautogui.hotkey(*keys)
        elif action == "keydown":
            pyautogui.keyDown(cmd.get("key", ""))
        elif action == "keyup":
            pyautogui.keyUp(cmd.get("key", ""))
        elif action == "press":
            pyautogui.press(cmd.get("key", ""))
        send_json(sock, {"type": "ok"})
    except Exception as e:
        send_json(sock, {"type": "error", "msg": str(e)})


def handle_exec(sock: socket.socket, cmd: dict):
    """Выполняет Python-код в фоне и возвращает вывод."""
    code = cmd.get("code", "")
    timeout = cmd.get("timeout", 10)

    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()

    result = {"type": "exec_result", "stdout": "", "stderr": "", "error": ""}
    try:
        exec_globals = {}
        exec(compile(code, "<remote>", "exec"), exec_globals)
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        result["stdout"] = sys.stdout.getvalue()
        result["stderr"] = sys.stderr.getvalue()
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    send_json(sock, result)


def handle_shell(sock: socket.socket, cmd: dict):
    """Выполняет shell-команду."""
    command = cmd.get("command", "")
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=15
        )
        send_json(sock, {
            "type": "shell_result",
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        })
    except subprocess.TimeoutExpired:
        send_json(sock, {"type": "error", "msg": "Timeout"})
    except Exception as e:
        send_json(sock, {"type": "error", "msg": str(e)})


def handle_file_list(sock: socket.socket, cmd: dict):
    """Список файлов в директории."""
    path = cmd.get("path", os.path.expanduser("~"))
    try:
        items = []
        for entry in os.scandir(path):
            items.append({
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size": entry.stat().st_size if entry.is_file() else 0,
            })
        send_json(sock, {"type": "file_list", "path": path, "items": items})
    except Exception as e:
        send_json(sock, {"type": "error", "msg": str(e)})


def handle_file_download(sock: socket.socket, cmd: dict):
    """Отправляет файл серверу."""
    path = cmd.get("path", "")
    try:
        with open(path, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode()
        send_json(sock, {
            "type": "file_data",
            "name": os.path.basename(path),
            "size": len(data),
            "data": b64,
        })
    except Exception as e:
        send_json(sock, {"type": "error", "msg": str(e)})


def handle_file_upload(sock: socket.socket, cmd: dict):
    """Принимает файл от сервера и сохраняет."""
    path = cmd.get("path", "")
    b64 = cmd.get("data", "")
    try:
        data = base64.b64decode(b64)
        with open(path, "wb") as f:
            f.write(data)
        send_json(sock, {"type": "ok", "msg": f"Saved {len(data)} bytes to {path}"})
    except Exception as e:
        send_json(sock, {"type": "error", "msg": str(e)})


def handle_camera(sock: socket.socket, cmd: dict):
    """Захватывает один кадр с веб-камеры."""
    if not CV2_OK:
        send_json(sock, {"type": "error", "msg": "opencv not installed"})
        return
    cam_id = cmd.get("cam_id", 0)
    cap = cv2.VideoCapture(cam_id)
    ret, frame = cap.read()
    cap.release()
    if not ret:
        send_json(sock, {"type": "error", "msg": "Camera not available"})
        return
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    data = buf.tobytes()
    send_json(sock, {"type": "camera_frame", "size": len(data)})
    send_packet(sock, data)


def handle_sysinfo(sock: socket.socket, _cmd: dict):
    """Возвращает системную информацию."""
    import platform
    info = {
        "type": "sysinfo",
        "os": platform.system(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "hostname": socket.gethostname(),
        "python": sys.version,
        "cwd": os.getcwd(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER", "unknown"),
    }
    try:
        import psutil
        info["cpu_percent"] = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        info["ram_total_mb"] = mem.total // (1024 * 1024)
        info["ram_used_mb"]  = mem.used  // (1024 * 1024)
    except ImportError:
        pass
    send_json(sock, info)


# ─────────────────────────── стриминг экрана ────────────────────

class ScreenStreamThread(threading.Thread):
    def __init__(self, host: str, port: int):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.running = False

    def run(self):
        self.running = True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((self.host, self.port))
            send_json(s, {"type": "stream_hello"})
            interval = 1.0 / SCREEN_FPS
            while self.running:
                t0 = time.time()
                if MSS_OK and PIL_OK:
                    with mss.mss() as sct:
                        monitor = sct.monitors[1]
                        img = sct.grab(monitor)
                        pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
                        # масштаб 50% для снижения трафика
                        pil = pil.resize((pil.width // 2, pil.height // 2))
                        buf = io.BytesIO()
                        pil.save(buf, format="JPEG", quality=40)
                        data = buf.getvalue()
                    send_json(s, {"type": "frame", "size": len(data)})
                    send_packet(s, data)
                elapsed = time.time() - t0
                time.sleep(max(0, interval - elapsed))
        except Exception as e:
            print(f"[stream] stopped: {e}")
        finally:
            self.running = False


# ─────────────────────────── стриминг камеры ────────────────────

class CameraStreamThread(threading.Thread):
    def __init__(self, host: str, port: int, cam_id: int = 0):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.cam_id = cam_id
        self.running = False

    def run(self):
        if not CV2_OK:
            return
        self.running = True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((self.host, self.port))
            send_json(s, {"type": "cam_hello"})
            cap = cv2.VideoCapture(self.cam_id)
            interval = 1.0 / CAMERA_FPS
            while self.running:
                t0 = time.time()
                ret, frame = cap.read()
                if ret:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
                    data = buf.tobytes()
                    send_json(s, {"type": "cam_frame", "size": len(data)})
                    send_packet(s, data)
                elapsed = time.time() - t0
                time.sleep(max(0, interval - elapsed))
            cap.release()
        except Exception as e:
            print(f"[camera] stopped: {e}")
        finally:
            self.running = False


# ─────────────────────────── главный цикл ───────────────────────

HANDLERS = {
    "screenshot":     handle_screenshot,
    "mouse":          handle_mouse,
    "keyboard":       handle_keyboard,
    "exec":           handle_exec,
    "shell":          handle_shell,
    "file_list":      handle_file_list,
    "file_download":  handle_file_download,
    "file_upload":    handle_file_upload,
    "camera":         handle_camera,
    "sysinfo":        handle_sysinfo,
}

screen_stream: ScreenStreamThread | None = None
camera_stream: CameraStreamThread | None = None


def main():
    global screen_stream, camera_stream

    while True:
        try:
            print(f"[client] Подключение к {SERVER_HOST}:{SERVER_PORT}...")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((SERVER_HOST, SERVER_PORT))
            print("[client] Подключено.")
            send_json(sock, {"type": "hello", "hostname": socket.gethostname()})

            while True:
                cmd = recv_json(sock)
                if not cmd:
                    break
                t = cmd.get("type", "")
                print(f"[client] команда: {t}")

                if t == "start_stream":
                    port = cmd.get("port", SERVER_PORT + 1)
                    screen_stream = ScreenStreamThread(SERVER_HOST, port)
                    screen_stream.start()
                    send_json(sock, {"type": "ok"})

                elif t == "stop_stream":
                    if screen_stream:
                        screen_stream.running = False
                    send_json(sock, {"type": "ok"})

                elif t == "start_cam_stream":
                    port = cmd.get("port", SERVER_PORT + 2)
                    camera_stream = CameraStreamThread(SERVER_HOST, port, cmd.get("cam_id", 0))
                    camera_stream.start()
                    send_json(sock, {"type": "ok"})

                elif t == "stop_cam_stream":
                    if camera_stream:
                        camera_stream.running = False
                    send_json(sock, {"type": "ok"})

                elif t in HANDLERS:
                    HANDLERS[t](sock, cmd)

                else:
                    send_json(sock, {"type": "error", "msg": f"Unknown command: {t}"})

        except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
            print(f"[client] Ошибка: {e}. Повтор через 5 сек...")
            time.sleep(5)
        except KeyboardInterrupt:
            print("[client] Остановлен.")
            break


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=SERVER_HOST)
    ap.add_argument("--port", type=int, default=SERVER_PORT)
    args = ap.parse_args()
    SERVER_HOST = args.host
    SERVER_PORT = args.port
    main()
