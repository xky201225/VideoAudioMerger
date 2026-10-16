import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from email.parser import BytesParser
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
TEMP_DIR = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "outputs"
FFMPEG_EXE = BASE_DIR / "ffmpeg.exe"

for _d in (TEMP_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".m4s",
    ".ts", ".m2ts", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".aac", ".m4a", ".m4s", ".wav", ".flac", ".ogg", ".opus",
    ".wma", ".aiff", ".aif", ".alac", ".ac3", ".mka",
}
FORMAT_EXTENSIONS = {
    "mp4": ".mp4",
    "mkv": ".mkv",
    "mov": ".mov",
    "webm": ".webm",
    "avi": ".avi",
    "m4v": ".m4v",
}

MP4_VIDEO = {"h264", "hevc", "h265", "mpeg4", "av1"}
MP4_AUDIO = {"aac", "mp3", "alac", "ac3", "eac3"}
WEBM_VIDEO = {"vp8", "vp9", "av1"}
WEBM_AUDIO = {"vorbis", "opus"}


JOBS = {}
JOBS_LOCK = threading.Lock()


def sanitize_name(name):
    name = Path(name or "output").stem
    name = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name, flags=re.UNICODE)
    return name.strip("._") or "output"


def ffmpeg_version():
    try:
        result = subprocess.run(
            [str(FFMPEG_EXE), "-version"],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=5,
        )
        first = (result.stdout or result.stderr).splitlines()
        return first[0].strip() if first else "FFmpeg"
    except Exception:
        return "FFmpeg"


def run_probe(path):
    if not Path(path).exists():
        return None
    result = subprocess.run(
        [str(FFMPEG_EXE), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        timeout=20,
    )
    info = (result.stderr or "") + "\n" + (result.stdout or "")
    duration_match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info
    )
    duration_ms = 0
    if duration_match:
        h, m, s = duration_match.groups()
        duration_ms = int(h) * 3600000 + int(m) * 60000 + int(float(s) * 1000)

    video_codec = None
    audio_codec = None
    for line in info.splitlines():
        vmatch = re.search(r"Video:\s*([^\s,(]+)", line)
        if vmatch and not video_codec:
            video_codec = vmatch.group(1).lower()
        amatch = re.search(r"Audio:\s*([^\s,(]+)", line)
        if amatch and not audio_codec:
            audio_codec = amatch.group(1).lower()

    return {
        "duration_ms": duration_ms,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
    }


def choose_format(video_codec, audio_codec):
    video_codec = (video_codec or "").lower()
    audio_codec = (audio_codec or "").lower()
    if video_codec in MP4_VIDEO and audio_codec in MP4_AUDIO:
        return "mp4"
    if video_codec in WEBM_VIDEO and audio_codec in WEBM_AUDIO:
        return "webm"
    return "mkv"


def parse_progress_time(value):
    match = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value)
    if not match:
        return None
    h, m, s = match.groups()
    return int(h) * 3600000 + int(m) * 60000 + int(float(s) * 1000)


def set_job(job_id, **changes):
    with JOBS_LOCK:
        job = JOBS.setdefault(job_id, {})
        job.update(changes)


def get_job(job_id):
    with JOBS_LOCK:
        return dict(JOBS.get(job_id, {}))


def process_job(job_id, video_path, audio_path, output_format, video_name):
    try:
        set_job(job_id, status="probing", progress=1, message="正在读取媒体信息")
        video_info = run_probe(video_path)
        audio_info = run_probe(audio_path)

        if not video_info or not video_info.get("video_codec"):
            raise RuntimeError("未能识别视频文件中的视频流，请确认上传的是有效视频。")
        if not audio_info or not audio_info.get("audio_codec"):
            raise RuntimeError("未能识别音频文件中的音频流，请确认上传的是有效音频。")

        ext = ".mkv"
        if output_format == "auto":
            output_format = choose_format(
                video_info["video_codec"], audio_info["audio_codec"]
            )
        ext = FORMAT_EXTENSIONS.get(output_format, ".mkv")

        safe_stem = sanitize_name(video_name)
        output_path = OUTPUT_DIR / f"{safe_stem}_{job_id}{ext}"
        download_name = f"{safe_stem}_merged{ext}"

        cmd = [
            str(FFMPEG_EXE),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-map_metadata",
            "0",
            "-progress",
            "pipe:1",
            "-nostats",
        ]
        if output_format in ("mp4", "mov", "m4v"):
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(output_path))

        set_job(job_id, status="processing", progress=3, message="正在无损封装")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )

        stderr_lines = []

        def drain_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        duration_ms = video_info.get("duration_ms") or 0
        last_progress = 3
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time="):
                current_ms = parse_progress_time(line.split("=", 1)[1])
                if current_ms is not None and duration_ms:
                    progress = min(99, 3 + int(current_ms / duration_ms * 96))
                    if progress > last_progress:
                        last_progress = progress
                        set_job(job_id, progress=progress, message="正在无损封装")

        proc.wait()
        stderr_thread.join(timeout=2)

        if proc.returncode != 0:
            tail = "\n".join(stderr_lines[-8:]).strip()
            raise RuntimeError(
                "无损封装失败。所选容器可能不支持当前音/视频编码，"
                "请改用“自动”或 MKV 后重试。\n" + tail
            )

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("输出文件未生成，请重试。")

        set_job(
            job_id,
            status="done",
            progress=100,
            message="合并完成",
            output_path=str(output_path),
            output_name=download_name,
            output_format=output_format,
        )
    except Exception as exc:
        set_job(job_id, status="error", progress=100, message=str(exc))
    finally:
        for path in (video_path, audio_path):
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass


class MultipartReader:
    def __init__(self, stream, content_length, boundary):
        self.stream = stream
        self.remaining = content_length
        self.boundary = boundary
        self.delimiter = b"\r\n--" + boundary
        self.buf = b""

    def _read(self, size):
        if size <= 0 or self.remaining <= 0:
            return b""
        size = min(size, self.remaining)
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data

    def read_until(self, needle):
        out = bytearray()
        while True:
            idx = self.buf.find(needle)
            if idx != -1:
                out.extend(self.buf[:idx])
                self.buf = self.buf[idx + len(needle):]
                return bytes(out)
            out.extend(self.buf)
            self.buf = b""
            if self.remaining <= 0:
                return None
            chunk = self._read(65536)
            if not chunk:
                return None
            self.buf += chunk

    def consume_until(self, needle, write_callback):
        keep = len(needle) - 1
        while True:
            idx = self.buf.find(needle)
            if idx != -1:
                if idx:
                    write_callback(self.buf[:idx])
                self.buf = self.buf[idx + len(needle):]
                return True
            if len(self.buf) > keep:
                write_callback(self.buf[:len(self.buf) - keep])
                self.buf = self.buf[len(self.buf) - keep:]
            if self.remaining <= 0:
                if self.buf:
                    write_callback(self.buf)
                    self.buf = b""
                return False
            chunk = self._read(65536)
            if not chunk:
                return False
            self.buf += chunk


def parse_multipart(stream, content_length, boundary):
    reader = MultipartReader(stream, content_length, boundary)
    first = reader.read_until(b"\r\n")
    if first is None or first != b"--" + boundary:
        raise ValueError("无效的 multipart 请求")

    fields = {}
    files = {}
    file_names = {}

    while True:
        header_block = reader.read_until(b"\r\n\r\n")
        if header_block is None:
            break

        msg = BytesParser(policy=policy.default).parsebytes(
            header_block + b"\r\n\r\n"
        )
        name = msg.get_param("name", header="content-disposition") or ""
        filename = msg.get_filename()

        if filename:
            path = TEMP_DIR / f"{uuid.uuid4().hex}_{Path(filename).name}"
            with open(path, "wb") as handle:
                reader.consume_until(reader.delimiter, handle.write)
            files[name] = path
            file_names[name] = filename
        else:
            value = reader.read_until(reader.delimiter)
            fields[name] = (value or b"").decode("utf-8", errors="replace")

        if reader.buf.startswith(b"--"):
            reader.buf = b""
            break
        if reader.buf.startswith(b"\r\n"):
            reader.buf = reader.buf[2:]

    return fields, files, file_names


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoAudioMerger/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, download_name=None):
        path = Path(path)
        if not path.exists():
            self.send_json(404, {"ok": False, "error": "文件不存在或已被清理"})
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        length = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if download_name:
            from urllib.parse import quote

            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quote(download_name)}",
            )
        self.end_headers()
        with open(path, "rb") as handle:
            shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_file(WEB_DIR / "index.html")
            return
        if parsed.path.startswith("/static/"):
            relative = parsed.path[len("/static/"):]
            target = (WEB_DIR / relative).resolve()
            if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
                self.send_json(404, {"ok": False, "error": "Not found"})
                return
            self.send_file(target)
            return
        if parsed.path == "/api/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "ffmpeg": ffmpeg_version(),
                    "video_extensions": sorted(VIDEO_EXTENSIONS),
                    "audio_extensions": sorted(AUDIO_EXTENSIONS),
                    "formats": sorted(FORMAT_EXTENSIONS),
                },
            )
            return
        if parsed.path == "/api/progress":
            query = parse_qs(parsed.query)
            job_id = (query.get("job") or [""])[0]
            job = get_job(job_id)
            if not job:
                self.send_json(404, {"ok": False, "error": "任务不存在"})
                return
            self.send_json(200, {"ok": True, "job": job})
            return
        if parsed.path == "/download":
            query = parse_qs(parsed.query)
            job_id = (query.get("job") or [""])[0]
            job = get_job(job_id)
            if not job or job.get("status") != "done":
                self.send_json(404, {"ok": False, "error": "文件不存在"})
                return
            self.send_file(job["output_path"], job.get("output_name"))
            return
        self.send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/merge":
            self.send_json(404, {"ok": False, "error": "Not found"})
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self.send_json(400, {"ok": False, "error": "需要 multipart/form-data"})
            return

        boundary_match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type)
        if not boundary_match:
            self.send_json(400, {"ok": False, "error": "缺少 boundary"})
            return
        boundary = (boundary_match.group(1) or boundary_match.group(2)).strip().encode()

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                self.send_json(411, {"ok": False, "error": "缺少 Content-Length"})
                return
            fields, files, file_names = parse_multipart(
                self.rfile, length, boundary
            )
        except Exception as exc:
            self.send_json(400, {"ok": False, "error": f"上传解析失败：{exc}"})
            return

        video_path = files.get("video")
        audio_path = files.get("audio")
        if not video_path or not audio_path:
            self.send_json(400, {"ok": False, "error": "请同时选择视频和音频文件"})
            return

        output_format = (fields.get("format") or "auto").strip().lower()
        if output_format not in FORMAT_EXTENSIONS and output_format != "auto":
            output_format = "auto"

        job_id = uuid.uuid4().hex
        set_job(
            job_id,
            status="queued",
            progress=0,
            message="等待处理",
            video_name=file_names.get("video", "video"),
            audio_name=file_names.get("audio", "audio"),
            created=time.time(),
        )
        thread = threading.Thread(
            target=process_job,
            args=(job_id, video_path, audio_path, output_format, file_names.get("video", "video")),
            daemon=True,
        )
        thread.start()
        self.send_json(202, {"ok": True, "job": job_id})


def main():
    if not FFMPEG_EXE.exists():
        print("错误：缺少 ffmpeg.exe，请确认它与 app.py 位于同一目录。")
        return

    start_port = 8765
    if "--port" in sys.argv:
        try:
            start_port = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            print("端口参数无效，使用默认端口 8765。")

    port = start_port
    for candidate in range(start_port, start_port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            server.daemon_threads = True
            port = candidate
            break
        except OSError:
            continue
    else:
        print(f"错误：{start_port}-{start_port + 19} 端口均被占用。")
        return

    url = f"http://127.0.0.1:{port}"
    print(f"无损音视频合并工具已启动：{url}")
    print("按 Ctrl+C 停止服务。")
    if "--no-browser" not in sys.argv and not os.environ.get(
        "VIDEO_AUDIO_MERGER_NO_BROWSER"
    ):
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
