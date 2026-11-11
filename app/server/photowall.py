#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞牛 fnOS 瀑布流相册 —— 后端服务

设计要点：
  * 零第三方依赖，只用 Python 3 标准库，便于在 fnOS 上直接运行；
  * 通过统一网关（Unix Socket）对外提供服务，登录态由飞牛网关校验；
  * 只允许访问「用户授权目录」(TRIM_DATA_ACCESSIBLE_PATHS)，所有路径经过
    realpath 归一化 + 前缀校验，拒绝目录穿越；
  * 缩略图三级降级：Pillow -> ffmpeg -> 原图直出；
  * 图片尺寸直接解析文件头（不整图解码），保证瀑布流首屏稳定不跳动。
"""

import argparse
import bisect
import collections
import hashlib
import heapq
import json
import os
import pickle
import random
import re
import shutil
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

IMAGE_EXTS = {
    "jpg", "jpeg", "jpe", "jfif", "png", "gif", "webp", "bmp", "avif",
    "heic", "heif", "tif", "tiff", "jxl",
}
VIDEO_EXTS = {
    "mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "flv", "ts", "m2ts",
    "mts", "mpg", "mpeg", "3gp", "ogv", "rmvb", "rm",
}

KIND = {}
for _e in IMAGE_EXTS:
    KIND[_e] = "image"
for _e in VIDEO_EXTS:
    KIND[_e] = "video"

MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "jpe": "image/jpeg",
    "jfif": "image/jpeg", "png": "image/png", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp", "avif": "image/avif",
    "heic": "image/heic", "heif": "image/heif", "tif": "image/tiff",
    "tiff": "image/tiff", "jxl": "image/jxl",
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
    "mkv": "video/x-matroska", "webm": "video/webm", "avi": "video/x-msvideo",
    "wmv": "video/x-ms-wmv", "flv": "video/x-flv", "ts": "video/mp2t",
    "m2ts": "video/mp2t", "mts": "video/mp2t", "mpg": "video/mpeg",
    "mpeg": "video/mpeg", "3gp": "video/3gpp", "ogv": "video/ogg",
    "rmvb": "video/vnd.rn-realvideo", "rm": "video/vnd.rn-realvideo",
}

SKIP_DIRS = {
    "@eaDir", "#recycle", "@tmp", "@appstore", "@appdata", "@appconf",
    "@apphome", "@apptemp", "$RECYCLE.BIN", "System Volume Information",
    "lost+found", ".Trash", ".trash", "@thumb", "@transcode", "@database",
}

STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json",
}

THUMB_WIDTHS = (160, 240, 320, 480, 640, 960, 1280, 1920, 2560)
DEFAULT_THUMB = 480
PAGE_SIZE = 250                            # 每次瀑布流返回的照片数
THUMB_CACHE_LIMIT = 10 * 1024 ** 3         # 缩略图缓存上限 10 GB
THUMB_MIN_KEEP = 5000                      # 保底保留 5000 张 ≈ 20 屏 × 250 张
THUMB_PRUNE_INTERVAL = 600                 # 每 10 分钟巡检一次缓存
THUMB_PRUNE_RATIO = 0.85                   # 触发上限时清理到 85%
THUMB_MAX_AGE = 60 * 60 * 24 * 60          # 未被访问 60 天的缩略图优先淘汰
PREFETCH_MAX = 900                         # 预生成队列上限
PAGE_SIZE_FIRST = 60                       # 前端每页 60 张
LOOKAHEAD_ITEMS = 150                      # 额外预生成下一页的缩略图
INDEX_TTL = 120                            # 索引缓存有效期（秒）——仅用于判断快照是否需要后台刷新
INDEX_SNAPSHOT_VERSION = 1                 # 索引磁盘快照格式版本
INDEX_POLITE_EVERY = 40                    # 后台扫描时每遍历多少个目录让一次路

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

try:  # Pillow 可选：存在时缩略图质量最好
    from PIL import Image, ImageOps  # type: ignore
    PIL_OK = True
except Exception:  # pragma: no cover
    PIL_OK = False

DEBUG = bool(os.environ.get("PHOTOWALL_DEBUG"))
DEBUG_NO_SENDFILE = bool(os.environ.get("PHOTOWALL_NO_SENDFILE"))


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


# --------------------------------------------------------------------------
# 路径工具
# --------------------------------------------------------------------------

def normcase(p):
    return os.path.normcase(os.path.abspath(p))


def within(path, base):
    """path 是否位于 base 之内（含 base 本身）。"""
    try:
        p = normcase(path)
        b = normcase(base)
        if p == b:
            return True
        return os.path.commonpath([p, b]) == b
    except (ValueError, OSError):
        return False


def human_size(n):
    if n is None:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    if i == 0:
        return "%d B" % n
    return ("%.2f" % v).rstrip("0").rstrip(".") + " " + units[i]


def ext_of(name):
    i = name.rfind(".")
    if i <= 0:
        return ""
    return name[i + 1:].lower()


def kind_of(name):
    return KIND.get(ext_of(name))


def quote(s):
    return urllib.parse.quote(s, safe="")


# --------------------------------------------------------------------------
# 图片尺寸：只读文件头，速度快
# --------------------------------------------------------------------------

def image_dims(path, ext):
    try:
        with open(path, "rb") as f:
            if ext in ("jpg", "jpeg", "jpe", "jfif"):
                if f.read(2) != b"\xff\xd8":
                    return None
                while True:
                    b = f.read(1)
                    if not b:
                        return None
                    if b != b"\xff":
                        continue
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if not marker:
                        return None
                    m = marker[0]
                    if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                        continue
                    if m == 0xD9:
                        return None
                    raw = f.read(2)
                    if len(raw) < 2:
                        return None
                    seglen = struct.unpack(">H", raw)[0]
                    if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                        data = f.read(5)
                        if len(data) < 5:
                            return None
                        h, w = struct.unpack(">HH", data[1:5])
                        return (w, h) if w and h else None
                    f.seek(seglen - 2, 1)
            elif ext == "png":
                head = f.read(26)
                if head[:8] != b"\x89PNG\r\n\x1a\n":
                    return None
                w, h = struct.unpack(">II", head[16:24])
                return (w, h) if w and h else None
            elif ext == "gif":
                head = f.read(10)
                if head[:3] != b"GIF":
                    return None
                w, h = struct.unpack("<HH", head[6:10])
                return (w, h) if w and h else None
            elif ext == "bmp":
                head = f.read(26)
                if head[:2] != b"BM":
                    return None
                w, h = struct.unpack("<ii", head[18:26])
                return (abs(w), abs(h)) if w and h else None
            elif ext == "webp":
                head = f.read(30)
                if head[:4] != b"RIFF" or head[8:12] != b"WEBP":
                    return None
                fourcc = head[12:16]
                if fourcc == b"VP8X":
                    w = int.from_bytes(head[24:27], "little") + 1
                    h = int.from_bytes(head[27:30], "little") + 1
                    return (w, h)
                if fourcc == b"VP8 ":
                    w = struct.unpack("<H", head[26:28])[0] & 0x3FFF
                    h = struct.unpack("<H", head[28:30])[0] & 0x3FFF
                    return (w, h) if w and h else None
                if fourcc == b"VP8L":
                    bits = int.from_bytes(head[21:25], "little")
                    w = (bits & 0x3FFF) + 1
                    h = ((bits >> 14) & 0x3FFF) + 1
                    return (w, h)
            elif ext in ("tif", "tiff"):
                head = f.read(16)
                bo = "<" if head[:2] == b"II" else ">"
                if struct.unpack(bo + "H", head[2:4])[0] != 42:
                    return None
                off = struct.unpack(bo + "I", head[4:8])[0]
                f.seek(off)
                cnt = struct.unpack(bo + "H", f.read(2))[0]
                w = h = None
                for _ in range(cnt):
                    entry = f.read(12)
                    if len(entry) < 12:
                        break
                    tag, typ = struct.unpack(bo + "HH", entry[:4])
                    count = struct.unpack(bo + "I", entry[4:8])[0]
                    val = entry[8:12]
                    if typ == 3:  # SHORT
                        v = struct.unpack(bo + "H", val[:2])[0]
                    elif typ == 4:  # LONG
                        v = struct.unpack(bo + "I", val)[0]
                    else:
                        continue
                    if tag == 256:
                        w = v
                    elif tag == 257:
                        h = v
                return (w, h) if w and h else None
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# EXIF 解析（纯 Python，支持 JPEG APP1 / TIFF 结构）
# --------------------------------------------------------------------------

TAG_NAMES = {
    0x010F: "Make", 0x0110: "Model", 0x0112: "Orientation",
    0x0132: "DateTime", 0x8769: "ExifIFD", 0x829A: "ExposureTime",
    0x829D: "FNumber", 0x8827: "ISOSpeedRatings", 0x9003: "DateTimeOriginal",
    0x9004: "DateTimeDigitized", 0x920A: "FocalLength", 0x9209: "Flash",
    0xA002: "PixelXDimension", 0xA003: "PixelYDimension",
    0xA405: "FocalLengthIn35mmFilm", 0xA433: "LensMake", 0xA434: "LensModel",
    0x9207: "MeteringMode", 0x8822: "ExposureProgram", 0x9204: "ExposureBiasValue",
    0xA40A: "ImageUniqueID",
}


def _read_exif_ifd(tiff, endian, offset, out, depth=0):
    if depth > 4 or offset <= 0 or offset >= len(tiff):
        return
    try:
        n = struct.unpack(endian + "H", tiff[offset:offset + 2])[0]
    except struct.error:
        return
    if n > 512:
        return
    for i in range(n):
        base = offset + 2 + i * 12
        chunk = tiff[base:base + 12]
        if len(chunk) < 12:
            return
        tag, typ, count = struct.unpack(endian + "HHI", chunk[:8])
        raw = chunk[8:12]

        def value_bytes():
            size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8}.get(typ, 0) * count
            if size <= 4:
                return raw[:size]
            voff = struct.unpack(endian + "I", raw)[0]
            return tiff[voff:voff + size]

        name = TAG_NAMES.get(tag)
        try:
            if typ in (3, 4, 8, 9) and name:
                vb = value_bytes()
                if len(vb) >= 2:
                    fmt = {3: "H", 4: "I", 8: "h", 9: "i"}[typ]
                    out[name] = struct.unpack(endian + fmt, vb[:struct.calcsize(fmt)])[0]
            elif typ in (5, 10) and name:
                vb = value_bytes()
                if len(vb) >= 8:
                    num, den = struct.unpack(endian + ("II" if typ == 5 else "ii"), vb[:8])
                    out[name] = (num, den)
            elif typ == 2 and name:
                vb = value_bytes().split(b"\x00")[0]
                out[name] = vb.decode("utf-8", "replace").strip()
        except Exception:
            pass

    # 递归 Exif 子 IFD / GPS
    for sub_tag in (0x8769, 0x8825):
        try:
            cnt = struct.unpack(endian + "H", tiff[offset:offset + 2])[0]
            for i in range(cnt):
                base = offset + 2 + i * 12
                chunk = tiff[base:base + 12]
                if len(chunk) < 12:
                    break
                tag = struct.unpack(endian + "H", chunk[:2])[0]
                if tag == sub_tag:
                    voff = struct.unpack(endian + "I", chunk[8:12])[0]
                    _read_exif_ifd(tiff, endian, voff, out, depth + 1)
        except Exception:
            pass


def read_exif(path, ext):
    if ext not in ("jpg", "jpeg", "jpe", "jfif", "tif", "tiff", "heic", "heif"):
        return {}
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    b = f.read(1)
                    if not b:
                        return {}
                    if b != b"\xff":
                        continue
                    m = f.read(1)
                    while m == b"\xff":
                        m = f.read(1)
                    if not m:
                        return {}
                    code = m[0]
                    if code in (0xD8, 0x01) or 0xD0 <= code <= 0xD7:
                        continue
                    raw = f.read(2)
                    if len(raw) < 2:
                        return {}
                    seglen = struct.unpack(">H", raw)[0]
                    if code == 0xE1:
                        payload = f.read(seglen - 2)
                        if payload[:6] == b"Exif\x00\x00":
                            return _parse_tiff(payload[6:])
                    else:
                        f.seek(seglen - 2, 1)
            else:
                f.seek(0)
                data = f.read(1024 * 256)
                idx = data.find(b"Exif\x00\x00")
                if idx > 0:
                    return _parse_tiff(data[idx + 6:])
    except Exception:
        return {}
    return {}


def _parse_tiff(tiff):
    if len(tiff) < 8:
        return {}
    endian = "<" if tiff[:2] == b"II" else ">"
    try:
        if struct.unpack(endian + "H", tiff[2:4])[0] != 42:
            return {}
        offset = struct.unpack(endian + "I", tiff[4:8])[0]
    except struct.error:
        return {}
    out = {}
    _read_exif_ifd(tiff, endian, offset, out)
    return out


def fmt_exif(ex):
    res = {}

    make = (ex.get("Make") or "").strip()
    model = (ex.get("Model") or "").strip()
    if model and make and not model.lower().startswith(make.lower()):
        camera = "%s %s" % (make, model)
    else:
        camera = model or make
    if camera:
        res["camera"] = camera

    lens = (ex.get("LensModel") or "").strip()
    if lens:
        res["lens"] = lens

    fnum = ex.get("FNumber")
    if isinstance(fnum, tuple) and fnum[1]:
        v = fnum[0] / fnum[1]
        res["aperture"] = ("f/%.1f" % v).replace(".0", "")

    et = ex.get("ExposureTime")
    if isinstance(et, tuple) and et[1]:
        v = et[0] / et[1]
        if v >= 1:
            res["shutter"] = ("%g" % v) + " 秒"
        else:
            res["shutter"] = "1/%d 秒" % round(1 / v)

    iso = ex.get("ISOSpeedRatings")
    if isinstance(iso, int) and iso:
        res["iso"] = "ISO %d" % iso

    fl = ex.get("FocalLength")
    if isinstance(fl, tuple) and fl[1]:
        v = fl[0] / fl[1]
        s = "%gmm" % v
        fl35 = ex.get("FocalLengthIn35mmFilm")
        if isinstance(fl35, int) and fl35 and abs(fl35 - v) > 0.5:
            s += "（等效 %dmm）" % fl35
        res["focal"] = s

    dto = ex.get("DateTimeOriginal") or ex.get("DateTimeDigitized") or ex.get("DateTime")
    if isinstance(dto, str) and len(dto) >= 10:
        res["shot_at"] = dto.replace(":", "-", 2)

    res["raw"] = {k: v for k, v in ex.items() if not isinstance(v, tuple)} if ex else {}
    return res


# --------------------------------------------------------------------------
# 缩略图
# --------------------------------------------------------------------------

class ThumbNails:
    """缩略图缓存。

    设计目标：
      * 首屏快：列表接口返回后立刻把这一页的缩略图排进预生成队列，用户滚到时已命中缓存；
      * 有保底：LRU 之外保证至少保留 THUMB_MIN_KEEP 张（约 20 屏 × 250 张），
        反复「换一批」不会把刚看过的图挤掉；
      * 不超限：默认 10 GB 上限，后台每 10 分钟巡检一次，超限按最久未访问优先淘汰。
    """

    def __init__(self, base_dir, workers=0, cache_limit=THUMB_CACHE_LIMIT,
                 min_keep=THUMB_MIN_KEEP):
        self.default_base = base_dir
        self.base = base_dir
        try:
            os.makedirs(self.base, exist_ok=True)
        except OSError as e:
            log("thumb dir unavailable: %s" % e)
            self.base = os.path.join("/tmp", "fnphotowall-thumbs")
            os.makedirs(self.base, exist_ok=True)

        self.workers = workers or min(6, max(3, (os.cpu_count() or 4)))
        self.cache_limit = int(cache_limit)
        self.min_keep = int(min_keep)
        self.pool = ThreadPoolExecutor(max_workers=self.workers,
                                       thread_name_prefix="thumb")
        self.guard = threading.Lock()
        self.inflight = {}                  # out_path -> Future
        self.queue = collections.deque()    # 预生成队列（新页在左，优先处理）
        self.queued = set()
        self.total = 0
        self.count = 0
        self.hits = 0
        self.misses = 0
        self.generated = 0
        self.ensured = set()                # 已确认存在的子目录，避免每次 makedirs
        self.have = set()                   # 已在磁盘上的缩略图路径，省掉一次 stat
        self._purge_flags = {}              # 正在清理的旧目录，key=normcase(path)
        self._stop = threading.Event()
        self.ffmpeg_sem = threading.Semaphore(max(2, min(4, self.workers)))

        self._scan_totals()
        threading.Thread(target=self._dispatcher, daemon=True,
                         name="thumb-dispatch").start()
        threading.Thread(target=self._housekeeper, daemon=True,
                         name="thumb-housekeep").start()
        log("thumb cache: %d files, %.1f MB, limit %.1f GB, workers %d, keep>=%d"
            % (self.count, self.total / 1048576.0, self.cache_limit / 1073741824.0,
               self.workers, self.min_keep))

    # ---------- 基础 ----------
    def _scan_totals(self):
        total = 0
        count = 0
        have = set()
        for dirpath, _dirnames, filenames in os.walk(self.base):
            for fn in filenames:
                if not fn.endswith(".jpg"):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    total += os.path.getsize(fp)
                    count += 1
                    have.add(fp)
                except OSError:
                    pass
        with self.guard:
            self.total = total
            self.count = count
            self.have = have

    def path_for(self, src, width, mtime=None):
        if mtime is None:
            try:
                mtime = int(os.stat(src).st_mtime)
            except OSError:
                return None
        key = hashlib.sha1(("%s|%d|%d" % (normcase(src), int(mtime), width)).encode("utf-8")).hexdigest()
        prefix = key[:2]
        sub = os.path.join(self.base, prefix)
        if prefix not in self.ensured:
            try:
                os.makedirs(sub, exist_ok=True)
                self.ensured.add(prefix)
            except OSError:
                return None
        return os.path.join(sub, "%s.jpg" % key)

    @staticmethod
    def _cached(out):
        try:
            return os.path.getsize(out) > 32
        except OSError:
            return False

    @staticmethod
    def _touch(out):
        try:
            os.utime(out, None)   # 更新时间戳用于 LRU
        except OSError:
            pass

    def is_cached(self, src, width, mtime=None):
        """这张图的缩略图是否已经在缓存池里（不触盘，只查内存集合）。"""
        out = self.path_for(src, width, mtime)
        return bool(out) and out in self.have

    # ---------- 任务调度 ----------
    def _submit(self, src, out, width, kind):
        with self.guard:
            fut = self.inflight.get(out)
            if fut is not None:
                return fut
            if self._cached(out):
                return None
            fut = self.pool.submit(self._job, src, out, width, kind)
            self.inflight[out] = fut
            return fut

    def _job(self, src, out, width, kind):
        try:
            if self._cached(out):
                return
            if self._generate(src, out, width, kind):
                try:
                    size = os.path.getsize(out)
                except OSError:
                    size = 0
                with self.guard:
                    self.total += size
                    self.count += 1
                    self.generated += 1
                    self.have.add(out)
        except Exception as e:
            if DEBUG:
                log("thumb job failed %s: %s" % (src, e))
        finally:
            with self.guard:
                self.inflight.pop(out, None)

    def _dispatcher(self):
        while not self._stop.is_set():
            item = None
            with self.guard:
                if self.queue and len(self.inflight) < self.workers * 2:
                    item = self.queue.popleft()
                    self.queued.discard(item[1])
            if item is None:
                self._stop.wait(0.08)
                continue
            src, out, width, kind = item
            if self._cached(out):
                continue
            self._submit(src, out, width, kind)

    # ---------- 对外接口 ----------
    def _generate(self, src, out, width, kind):
        """Pillow -> ffmpeg 两级生成，写临时文件后原子替换。"""
        tmp = out + ".tmp%d" % threading.get_ident()
        if PIL_OK:
            try:
                with Image.open(src) as im:
                    im.draft("RGB", (width * 2, width * 2))
                    im = ImageOps.exif_transpose(im)
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    im.thumbnail((width, width * 4), Image.LANCZOS)
                    im.save(tmp, "JPEG", quality=82, optimize=True, progressive=True)
                if os.path.getsize(tmp) > 32:
                    os.replace(tmp, out)
                    return True
            except Exception as e:
                if DEBUG:
                    log("PIL thumb failed %s: %s" % (src, e))
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass

        if FFMPEG:
            with self.ffmpeg_sem:
                args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
                if kind == "video":
                    args += ["-ss", "1", "-i", src, "-frames:v", "1"]
                else:
                    args += ["-i", src, "-frames:v", "1"]
                args += ["-vf", "scale='min(%d,iw)':-2" % width,
                         "-q:v", "4", "-f", "image2", tmp]
                try:
                    subprocess.run(args, timeout=45,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if os.path.exists(tmp) and os.path.getsize(tmp) > 32:
                        os.replace(tmp, out)
                        return True
                except Exception as e:
                    if DEBUG:
                        log("ffmpeg thumb failed %s: %s" % (src, e))
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
        return False

    def get(self, src, width, kind, wait=True):
        """返回缩略图路径；未命中则后台生成并（可选）等待完成。"""
        out = self.path_for(src, width)
        if not out:
            return None
        if self._cached(out):
            with self.guard:
                self.hits += 1
            self._touch(out)
            return out

        with self.guard:
            self.misses += 1
        fut = self._submit(src, out, width, kind)
        if fut is None:
            return out if self._cached(out) else None
        if not wait:
            return None
        try:
            fut.result(timeout=90)
        except Exception:
            return None
        return out if self._cached(out) else None

    def prefetch(self, src, width, kind, mtime=None, front=True):
        """front=True 插到队首（用户马上要看的），False 排到队尾（提前量）。"""
        out = self.path_for(src, width, mtime)
        if not out:
            return
        with self.guard:
            if out in self.have or out in self.inflight or out in self.queued:
                return
            if len(self.queue) >= PREFETCH_MAX:
                dropped = self.queue.pop() if front else self.queue.popleft()
                self.queued.discard(dropped[1])
            if front:
                self.queue.appendleft((src, out, width, kind))
            else:
                self.queue.append((src, out, width, kind))
            self.queued.add(out)

    def prefetch_many(self, items, width=DEFAULT_THUMB, video_limit=80, front=True):
        """把一页媒体排进预生成队列：图片全排，视频封面只排前若干个（ffmpeg 较慢）。"""
        if not items:
            return
        v = 0
        for it in items:
            if it.get("kind") == "video":
                if v >= video_limit:
                    continue
                v += 1
            self.prefetch(it["path"], width, it.get("kind") or "image",
                          it.get("mtime"), front)

    def stats(self):
        with self.guard:
            return {
                "count": self.count,
                "bytes": self.total,
                "bytesText": human_size(self.total),
                "limit": self.cache_limit,
                "limitText": human_size(self.cache_limit),
                "limitGb": round(self.cache_limit / 1073741824.0, 2),
                "minKeep": self.min_keep,
                "workers": self.workers,
                "queued": len(self.queue),
                "inflight": len(self.inflight),
                "hits": self.hits,
                "misses": self.misses,
                "generated": self.generated,
            }

    def set_limit_gb(self, gb):
        """设置缓存上限（GB），立即按新上限清理一次。"""
        try:
            gb = float(gb)
        except (TypeError, ValueError):
            raise ValueError("缓存上限必须是数字")
        gb = max(0.5, min(200.0, gb))
        self.cache_limit = int(gb * 1073741824)
        log("thumb cache limit -> %.2f GB" % gb)
        result = self.prune()
        return {"limitGb": gb, "prune": result, "stats": self.stats()}

    def switch_base(self, new_dir, clean_old=True):
        """切换缓存目录；旧目录里的缩略图在后台清掉（都可重新生成）。"""
        new_dir = os.path.abspath(new_dir)
        old = os.path.abspath(self.base)
        if normcase(new_dir) == normcase(old):
            return {"changed": False, "base": self.base}
        # 如果目标目录正在被「旧缓存清理」删除，先取消那次清理
        pending = self._purge_flags.get(normcase(new_dir))
        if pending:
            pending.set()
        try:
            os.makedirs(new_dir, exist_ok=True)
            probe = os.path.join(new_dir, ".write-test%d" % os.getpid())
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as e:
            raise ValueError("该目录不可写：%s" % e)

        with self.guard:
            self.base = new_dir
            self.ensured = {name[:2] for name in os.listdir(new_dir) if len(name) == 2} \
                if os.path.isdir(new_dir) else set()
            self.have = set()
            self.total = 0
            self.count = 0
        self._scan_totals()
        log("thumb cache dir -> %s" % new_dir)

        if clean_old and old and normcase(old) != normcase(new_dir) and os.path.isdir(old):
            ev = threading.Event()
            self._purge_flags[normcase(old)] = ev
            threading.Thread(target=self._purge_dir, args=(old, ev), daemon=True).start()
        return {"changed": True, "base": new_dir, "stats": self.stats()}

    def _purge_dir(self, path, cancel):
        """删掉旧缓存目录内容（后台执行，失败不致命）。"""
        key = normcase(path)
        removed = 0
        try:
            for dirpath, dirnames, filenames in os.walk(path, topdown=False):
                if cancel.is_set():
                    return
                for fn in filenames:
                    if cancel.is_set():
                        return
                    try:
                        os.remove(os.path.join(dirpath, fn))
                        removed += 1
                    except OSError:
                        pass
                for dn in dirnames:
                    try:
                        os.rmdir(os.path.join(dirpath, dn))
                    except OSError:
                        pass
            if not cancel.is_set():
                try:
                    os.rmdir(path)
                except OSError:
                    pass
            log("old thumb cache purged: %d files (%s)" % (removed, path))
        except Exception as e:
            log("purge old cache failed: %s" % e)
        finally:
            self._purge_flags.pop(key, None)

    # ---------- 清理 ----------
    def _housekeeper(self):
        # 启动后先等一会儿再巡检，避免和首屏加载抢 IO
        self._stop.wait(90)
        while not self._stop.is_set():
            try:
                self.prune()
            except Exception as e:
                log("thumb prune error: %s" % e)
            self._stop.wait(THUMB_PRUNE_INTERVAL)

    def prune(self, limit=None, min_keep=None):
        limit = self.cache_limit if limit is None else limit
        min_keep = self.min_keep if min_keep is None else min_keep
        files = []
        total = 0
        now = time.time()
        for dirpath, _dirnames, filenames in os.walk(self.base):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if fn.endswith(".tmp") or ".tmp" in fn:
                    # 清理中断遗留的临时文件
                    if now - st.st_mtime > 3600:
                        try:
                            os.remove(fp)
                        except OSError:
                            pass
                    continue
                files.append((st.st_mtime, st.st_size, fp))
                total += st.st_size

        files.sort(key=lambda x: x[0])      # 最久未访问的排前面
        self.count = len(files)
        removed = 0
        freed = 0
        if total > limit:
            target = int(limit * THUMB_PRUNE_RATIO)
            for mtime, size, fp in files:
                if total <= target:
                    break
                if self.count - removed <= min_keep:
                    # 保底库存不能低于 min_keep
                    break
                try:
                    os.remove(fp)
                    self.have.discard(fp)
                    total -= size
                    freed += size
                    removed += 1
                except OSError:
                    pass
        # 超期未访问的（且在保底之外）顺手清掉
        if THUMB_MAX_AGE > 0:
            for mtime, size, fp in files:
                if self.count - removed <= min_keep:
                    break
                if now - mtime < THUMB_MAX_AGE:
                    break
                try:
                    os.remove(fp)
                    self.have.discard(fp)
                    total -= size
                    freed += size
                    removed += 1
                except OSError:
                    pass

        with self.guard:
            self.total = total
            self.count = len(files) - removed
        if removed:
            log("thumb cache pruned: -%d files, -%.1f MB -> %.1f MB / %.1f GB"
                % (removed, freed / 1048576.0, total / 1048576.0,
                   limit / 1073741824.0))
        return {"removed": removed, "freed": freed, "total": total, "count": self.count}

    def prune_async(self):
        threading.Thread(target=self.prune, daemon=True).start()

    def shutdown(self):
        self._stop.set()
        try:
            self.pool.shutdown(wait=False)
        except Exception:
            pass


class Preloader:
    """后台预加载：把所有照片的缩略图提前生成到缓存池，直到达到缓存上限。

    用独立的线程往 ThumbNails 的预生成队列里持续投喂，队列满了就等一会儿，
    这样不会和「用户正在看的那一屏」抢优先级（新页仍然插在队首）。
    """

    def __init__(self, library, thumbs):
        self.lib = library
        self.thumbs = thumbs
        self.thread = None
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()
        self.state = {
            "running": False,
            "total": 0,
            "processed": 0,
            "skipped": 0,
            "reason": "",
            "startedAt": 0,
            "finishedAt": 0,
        }

    def status(self):
        with self.lock:
            st = dict(self.state)
        st["cached"] = self.thumbs.count
        st["bytes"] = self.thumbs.total
        st["bytesText"] = human_size(self.thumbs.total)
        st["limit"] = self.thumbs.cache_limit
        st["limitText"] = human_size(self.thumbs.cache_limit)
        st["queue"] = len(self.thumbs.queue)
        st["full"] = self.thumbs.total >= self.thumbs.cache_limit
        total = st.get("total") or 0
        st["percent"] = int(min(100, (st["processed"] / total * 100)) if total else 0)
        return st

    def start(self):
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update({
                "running": True, "total": 0, "processed": 0, "skipped": 0,
                "reason": "", "startedAt": time.time(), "finishedAt": 0,
            })
        self.stop_ev.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="preload")
        self.thread.start()
        return True

    def stop(self, reason="user"):
        self.stop_ev.set()
        with self.lock:
            if self.state["running"]:
                self.state["running"] = False
                self.state["reason"] = reason
                self.state["finishedAt"] = time.time()
        return True

    def _run(self):
        try:
            items = self.lib.all_media("image")
            with self.lock:
                self.state["total"] = len(items)
            rnd = random.Random(20260101)
            rnd.shuffle(items)
            for it in items:
                if self.stop_ev.is_set():
                    return
                if self.thumbs.total >= self.thumbs.cache_limit:
                    self.stop("limit")
                    return
                # 队列里积压太多就先等等，避免把用户当屏的请求挤在后面
                waited = 0
                while len(self.thumbs.queue) > 240 and not self.stop_ev.is_set():
                    time.sleep(0.15)
                    waited += 0.15
                    if waited > 30:
                        break
                if self.thumbs.is_cached(it["path"], DEFAULT_THUMB, it["mtime"]):
                    with self.lock:
                        self.state["skipped"] += 1
                        self.state["processed"] += 1
                    continue
                self.thumbs.prefetch(it["path"], DEFAULT_THUMB, "image", it["mtime"])
                with self.lock:
                    self.state["processed"] += 1
            # 队列里还有活就等它跑完，别急着报「已完成」
            deadline = time.time() + 600
            while not self.stop_ev.is_set() and time.time() < deadline:
                if not self.thumbs.queue and not self.thumbs.inflight:
                    break
                time.sleep(0.2)
            self.stop("done")
        except Exception as e:
            log("preload failed: %s" % e)
            self.stop("error")


# --------------------------------------------------------------------------
# 媒体库
# --------------------------------------------------------------------------

class Library:
    def __init__(self, media_roots, fav_dir, data_dir):
        self.media_roots = [os.path.abspath(p) for p in media_roots if os.path.isdir(p)]
        self.fav_dir = os.path.abspath(fav_dir)
        self.thumbs = None      # main() 里注入，用于列表返回时预热缩略图
        self.data_dir = data_dir
        self.guard = threading.RLock()
        self.index = {}            # root -> {"ts":float,"items":[...],"paths":[...]}
        self._refreshing = set()   # 正在后台刷新的 root，避免重复扫描
        self._build_locks = {}     # root -> Lock，保证同一根同时只扫一次
        self.dims = {}             # path -> (w,h)
        self.vmeta = {}            # path -> {"w","h","duration"}
        self.fav_map = {}          # fav_path -> {"src","name","ts"}
        self.fav_src = {}          # src_path -> fav_path
        self.probe_pending = set()
        try:
            os.makedirs(self.fav_dir, exist_ok=True)
        except OSError as e:
            log("favorites dir not writable (%s), fallback to data dir: %s" % (self.fav_dir, e))
            self.fav_dir = os.path.join(data_dir, "favorites")
            os.makedirs(self.fav_dir, exist_ok=True)
        self._load_fav()
        self._load_vmeta()

    # ---------- 收藏记录 ----------
    def _fav_file(self):
        return os.path.join(self.data_dir, "favorites.json")

    def _load_fav(self):
        try:
            with open(self._fav_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.fav_map = data.get("map", {}) or {}
                self.fav_src = data.get("src", {}) or {}
        except Exception:
            self.fav_map = {}
            self.fav_src = {}

    def _save_fav(self):
        try:
            tmp = self._fav_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"map": self.fav_map, "src": self.fav_src}, f,
                          ensure_ascii=False, indent=1)
            os.replace(tmp, self._fav_file())
        except Exception as e:
            log("save favorites failed: %s" % e)

    # ---------- 视频元信息 ----------
    def _vmeta_file(self):
        return os.path.join(self.data_dir, "videometa.json")

    def _load_vmeta(self):
        try:
            with open(self._vmeta_file(), "r", encoding="utf-8") as f:
                self.vmeta = json.load(f)
        except Exception:
            self.vmeta = {}

    def _save_vmeta(self):
        try:
            tmp = self._vmeta_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.vmeta, f, ensure_ascii=False)
            os.replace(tmp, self._vmeta_file())
        except Exception:
            pass

    def probe_video(self, path):
        if not FFPROBE or path in self.probe_pending:
            return
        with self.guard:
            if path in self.vmeta or path in self.probe_pending:
                return
            self.probe_pending.add(path)

        def run():
            try:
                out = subprocess.run(
                    [FFPROBE, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height:format=duration",
                     "-of", "json", path],
                    capture_output=True, timeout=20)
                info = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
                streams = info.get("streams") or [{}]
                s = streams[0]
                meta = {
                    "w": s.get("width"), "h": s.get("height"),
                    "duration": float((info.get("format") or {}).get("duration") or 0) or None,
                }
                with self.guard:
                    self.vmeta[path] = meta
                    dirty = True
            except Exception:
                with self.guard:
                    self.vmeta[path] = {}
            finally:
                with self.guard:
                    self.probe_pending.discard(path)
                    self._save_vmeta()

        threading.Thread(target=run, daemon=True).start()

    # ---------- 索引 ----------
    def all_roots(self):
        roots = list(self.media_roots)
        if os.path.isdir(self.fav_dir) and not any(within(self.fav_dir, r) for r in roots):
            roots.append(self.fav_dir)
        return roots

    def is_allowed(self, path):
        real = os.path.realpath(path)
        for r in self.all_roots():
            if within(real, r):
                return True
        return False

    def _build(self, root, polite=False):
        """扫描目录建索引。polite=True 时主动给缩略图生成让路，避免抢 IO。

        调用方必须已在 _build_locks[root] 内，保证同一根不会被并发重复扫描。
        """
        items = []
        dirs_seen = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in SKIP_DIRS
                           and not d.startswith("@")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                kind = kind_of(fn)
                if not kind:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                items.append({
                    "path": full,
                    "name": fn,
                    "kind": kind,
                    "ext": ext_of(fn),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "rnd": random.getrandbits(63),
                })
            dirs_seen += 1
            # 后台扫描时：缩略图队列有积压就让路，别跟用户正在看的那屏抢磁盘
            if polite and dirs_seen % INDEX_POLITE_EVERY == 0:
                try:
                    backlog = len(self.thumbs.queue) + len(self.thumbs.inflight) if self.thumbs else 0
                except Exception:
                    backlog = 0
                time.sleep(0.08 if backlog > 8 else 0.01)
            if len(items) > 400000:
                break
        items.sort(key=lambda it: it["path"])
        paths = [it["path"] for it in items]
        return {"ts": time.time(), "items": items, "paths": paths}

    # ---------- 索引磁盘快照 ----------
    def _index_dir(self):
        d = os.path.join(self.data_dir, "index")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return None
        return d

    def _index_file(self, root):
        d = self._index_dir()
        if not d:
            return None
        h = hashlib.sha1(os.path.normcase(root).encode("utf-8", "ignore")).hexdigest()[:16]
        return os.path.join(d, h + ".idx")

    def _save_index(self, root, ent):
        """索引快照写盘。用列式 pickle：体积小、加载快（几万条目几十毫秒）。

        同时把「这条是否已有缩略图」也存下来——启动后首屏就能按缓存优先排序，
        不用再为几万条算一遍 SHA1（那是几百毫秒）。这里在后台线程里算，不挡任何请求。
        """
        fp = self._index_file(root)
        if not fp:
            return
        try:
            items = ent["items"]
            if self.thumbs:
                try:
                    is_cached = self.thumbs.is_cached
                    for it in items:
                        if "_c" not in it:
                            it["_c"] = is_cached(it["path"], DEFAULT_THUMB, it["mtime"])
                except Exception:
                    pass
            payload = {
                "v": INDEX_SNAPSHOT_VERSION,
                "root": root,
                "ts": ent["ts"],
                "cols": (
                    [it["path"] for it in items],
                    [it["name"] for it in items],
                    [it["kind"] for it in items],
                    [it["ext"] for it in items],
                    [it["size"] for it in items],
                    [it["mtime"] for it in items],
                    [it["rnd"] for it in items],
                    [1 if it.get("_c") else 0 for it in items],
                ),
            }
            tmp = fp + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump(payload, f, protocol=4)
            os.replace(tmp, fp)
        except Exception as e:
            log("index snapshot save failed (%s): %s" % (root, e))

    def _save_index_async(self, root, ent):
        threading.Thread(target=self._save_index, args=(root, ent), daemon=True).start()

    def _prune_index_snapshots(self, roots):
        """清掉已不在授权列表里的索引快照，避免换目录后留下孤儿文件。"""
        d = self._index_dir()
        if not d:
            return
        try:
            keep = set()
            for r in roots:
                fp = self._index_file(r)
                if fp:
                    keep.add(os.path.basename(fp))
            for fn in os.listdir(d):
                if fn.endswith((".idx", ".idx.tmp")) and fn not in keep:
                    try:
                        os.remove(os.path.join(d, fn))
                    except OSError:
                        pass
        except Exception:
            pass

    def _load_index(self, root):
        """读磁盘快照；失败或版本不符返回 None。"""
        fp = self._index_file(root)
        if not fp or not os.path.isfile(fp):
            return None
        try:
            with open(fp, "rb") as f:
                payload = pickle.load(f)
            if not isinstance(payload, dict) or payload.get("v") != INDEX_SNAPSHOT_VERSION:
                return None
            if os.path.normcase(payload.get("root", "")) != os.path.normcase(root):
                return None
            cols = payload["cols"]
            paths, names, kinds, exts, sizes, mtimes, rnds = cols[:7]
            cached = cols[7] if len(cols) > 7 else None
            items = []
            for i, (p, n, k, e, s, m, r) in enumerate(zip(paths, names, kinds, exts, sizes, mtimes, rnds)):
                it = {"path": p, "name": n, "kind": k, "ext": e,
                      "size": s, "mtime": m, "rnd": r}
                if cached is not None:
                    it["_c"] = bool(cached[i])
                items.append(it)
            ent = {"ts": payload.get("ts", 0), "items": items, "paths": paths}
            if cached is not None:
                # 标记已在快照里，30 秒内不必重算；到期后由 _refresh_cached_flags 后台刷新
                ent["_cflag_ts"] = time.time()
            return ent
        except Exception as e:
            log("index snapshot load failed (%s): %s" % (root, e))
            return None

    def _lock_for(self, root):
        with self.guard:
            lk = self._build_locks.get(root)
            if lk is None:
                lk = self._build_locks[root] = threading.Lock()
            return lk

    def _build_once(self, root, polite=False):
        """同一根加锁扫一次；若别人刚好建好则直接复用。"""
        with self._lock_for(root):
            with self.guard:
                ent = self.index.get(root)
            if ent:
                return ent
            ent = self._build(root, polite=polite)
            with self.guard:
                self.index[root] = ent
            self._save_index_async(root, ent)   # 落盘放后台，别让首次请求等写文件
            return ent

    def _refresh_async(self, root):
        """后台重扫并原子替换索引；期间旧索引继续对外服务，请求不会被阻塞。"""
        with self.guard:
            if root in self._refreshing:
                return
            self._refreshing.add(root)

        def run():
            try:
                with self._lock_for(root):
                    ent = self._build(root, polite=True)
                    with self.guard:
                        self.index[root] = ent
                self._save_index(root, ent)
                log("index refreshed: %s (%d items)" % (root, len(ent["items"])))
            except Exception as e:
                log("index refresh failed (%s): %s" % (root, e))
            finally:
                with self.guard:
                    self._refreshing.discard(root)

        threading.Thread(target=run, daemon=True).start()

    def get_index(self, root, force=False):
        """拿到索引。

        关键：绝不因为「快照过期」而同步重扫——否则用户进相册会卡在扫描上。
        优先级：内存 → 磁盘快照（立即返回 + 后台刷新）→ 同步扫描（仅首次安装）。
        """
        with self.guard:
            ent = self.index.get(root)
            if ent and not force:
                return ent

        if not force:
            snap = self._load_index(root)
            if snap is not None:
                with self.guard:
                    self.index[root] = snap
                log("index snapshot hit: %s (%d items)" % (root, len(snap["items"])))
                self._refresh_async(root)      # 后台校正，不挡这次请求
                return snap

        return self._build_once(root, polite=False)

    def invalidate(self, root=None):
        with self.guard:
            if root:
                self.index.pop(root, None)
            else:
                self.index.clear()

    def invalidate_if_stale(self, seconds=20):
        """「换一批」会顺带刷新目录。

        改成后台重扫：不清空内存索引，否则下一次 /api/media 就要同步全盘扫描，
        用户点「换一批」得干等几秒到十几秒。旧索引继续服务，扫完自动原子替换。
        """
        now = time.time()
        if now - getattr(self, "_last_inval", 0) < seconds:
            return False
        self._last_inval = now
        for r in self.all_roots():
            self._refresh_async(r)
        return True

    def all_media(self, kinds="image"):
        out = []
        for r in self.media_roots:
            if not os.path.isdir(r):
                continue
            try:
                out.extend(self._ordered(r, kinds, 0))
            except Exception as e:
                log("all_media root %s failed: %s" % (r, e))
        return out

    def refresh_all(self):
        """把所有根排进后台重扫队列（不等结果，避免阻塞调用方）。"""
        for r in self.all_roots():
            try:
                self._refresh_async(r)
            except Exception:
                pass

    def warmup(self):
        """启动预热：先加载索引快照让首屏立刻可用，再后台校正。

        顺序很重要——
        1. 快照加载是几十毫秒级，加载完 /api/media 立刻能返回，缩略图直接命中磁盘缓存；
        2. 首次安装（无快照）才需要扫一次，用 polite 模式给缩略图让路；
        3. 有快照的根延迟几秒再后台重扫，确保首屏缩略图先跑完，不与它抢 IO。
        """
        def run():
            t0 = time.time()
            roots = self.all_roots()
            self._prune_index_snapshots(roots)

            # 1) 磁盘快照装进内存
            loaded, missing = 0, []
            for r in roots:
                snap = self._load_index(r)
                if snap is not None:
                    with self.guard:
                        self.index[r] = snap
                    loaded += 1
                else:
                    missing.append(r)
            log("index snapshot loaded %d/%d root(s) in %.3fs%s" % (
                loaded, len(roots), time.time() - t0,
                ("；首次需扫描: %d 个" % len(missing)) if missing else ""))

            # 2) 首次没有快照：必须扫一次，但让路给缩略图
            for r in missing:
                try:
                    self._build_once(r, polite=True)
                except Exception as e:
                    log("index initial build failed (%s): %s" % (r, e))

            # 3) 有快照且已经旧了：延迟几秒再后台校正，避免和首屏缩略图抢磁盘
            stale = []
            for r in roots:
                if r in missing:
                    continue
                with self.guard:
                    ent = self.index.get(r)
                if ent is not None and (time.time() - ent.get("ts", 0) > INDEX_TTL):
                    stale.append(r)
            if stale:
                def delayed():
                    time.sleep(3.0)
                    for r in stale:
                        self._refresh_async(r)
                threading.Thread(target=delayed, daemon=True).start()

            # 4) 预热首屏缩略图
            self.warmup_thumbs()

        threading.Thread(target=run, daemon=True).start()

    def warmup_thumbs(self, n=250):
        if not self.thumbs:
            return
        # 稍等一下再开工，避免刚启动就和首屏请求抢 CPU/IO
        time.sleep(2)
        picked = []
        for r in self.media_roots:
            try:
                picked.extend(self._ordered(r, "image", 0)[:n])
            except Exception:
                continue
            if len(picked) >= n:
                break
        if picked:
            self.thumbs.prefetch_many(picked[:n], DEFAULT_THUMB, video_limit=0)
            log("thumb warmup queued %d items" % min(len(picked), n))

    # ---------- 查询 ----------
    def _dims_for(self, item):
        path = item["path"]
        d = self.dims.get(path)
        if d:
            return d
        if item["kind"] == "image":
            d = image_dims(path, item["ext"]) or (0, 0)
            with self.guard:
                self.dims[path] = d
            return d
        m = self.vmeta.get(path)
        if m is not None:
            return (m.get("w") or 0, m.get("h") or 0) if m else (0, 0)
        self.probe_video(path)
        return (0, 0)

    def _ordered(self, root, kinds, seed, sub=None):
        ent = self.get_index(root)
        # 同一 seed 的排序结果复用，翻页时不用重新排 2 万条
        cache = ent.setdefault("_sorted", {})
        ckey = (kinds, seed, sub or "")
        hit = cache.get(ckey)
        if hit is not None:
            return hit
        items = ent["items"]
        if sub:
            lo = bisect.bisect_left(ent["paths"], sub + os.sep)
            hi = bisect.bisect_left(ent["paths"], sub + os.sep + "\U0010ffff")
            items = items[lo:hi]
        if kinds != "all":
            items = [it for it in items if it["kind"] == kinds]

        # 缓存优先：已在缩略图缓存池里的排在前面，打开就能秒出
        if self.thumbs and items:
            self._refresh_cached_flags(ent, items)
            out = sorted(items, key=lambda it: (0 if it.get("_c") else 1, it["rnd"] ^ seed))
        else:
            out = sorted(items, key=lambda it: it["rnd"] ^ seed)

        if len(cache) > 4:
            cache.clear()
        cache[ckey] = out
        return out

    def _refresh_cached_flags(self, ent, items):
        """缓存优先排序用的标记。

        「哪些图已有缩略图」要算每条的 SHA1，几万张就是几百毫秒——绝不能放在请求线程里，
        否则每次进相册都要多等半秒。这里改成后台算：算好清掉排序缓存，下一次请求自然用上新顺序。
        """
        now = time.time()
        if now - ent.get("_cflag_ts", 0) < 30:
            return
        ent["_cflag_ts"] = now          # 先占位，避免并发请求重复触发
        try:
            is_cached = self.thumbs.is_cached
        except Exception:
            return

        def run():
            try:
                for it in items:
                    it["_c"] = is_cached(it["path"], DEFAULT_THUMB, it["mtime"])
                ent.get("_sorted", {}).clear()
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def list_media(self, kinds, seed, offset, limit, sub=None, root=None):
        # 瀑布流只遍历授权目录，收藏夹不重复计入
        roots = [root] if root else self.media_roots
        lists = []
        for r in roots:
            if not os.path.isdir(r):
                continue
            try:
                lists.append(self._ordered(r, kinds, seed, sub))
            except Exception as e:
                log("list_media root %s failed: %s" % (r, e))
        if not lists:
            return {"total": 0, "items": []}
        if len(lists) == 1:
            merged = lists[0]
        else:
            merged = list(heapq.merge(
                *lists, key=lambda it: (0 if it.get("_c") else 1, it["rnd"] ^ seed)))
        total = len(merged)
        page = merged[offset:offset + limit] if limit else merged[offset:]
        # 本页缩略图插到队首（用户马上要），下一页排到队尾（提前量）
        if self.thumbs and page:
            try:
                self.thumbs.prefetch_many(page, DEFAULT_THUMB)
                end = offset + len(page)
                ahead = merged[end:end + LOOKAHEAD_ITEMS] if limit else []
                if ahead:
                    self.thumbs.prefetch_many(ahead, DEFAULT_THUMB,
                                              video_limit=20, front=False)
            except Exception:
                pass
        cached = sum(1 for it in page if it.get("_c"))
        return {"total": total, "cached": cached,
                "items": [self.serialize(it) for it in page]}

    def serialize(self, it, width=DEFAULT_THUMB):
        w, h = self._dims_for(it)
        q = quote(it["path"])
        data = {
            "path": it["path"],
            "name": it["name"],
            "kind": it["kind"],
            "ext": it["ext"],
            "size": it["size"],
            "mtime": it["mtime"],
            "w": w,
            "h": h,
            "fav": bool(self.fav_src.get(it["path"])),
            "thumb": "/api/thumb?path=%s&w=%d&v=%d" % (q, width, it["mtime"]),
            "src": "/api/file?path=%s" % q,
        }
        if it["kind"] == "video":
            with self.guard:
                m = self.vmeta.get(it["path"]) or {}
            if m.get("duration"):
                data["duration"] = round(float(m["duration"]), 1)
        return data

    # ---------- 文件夹 ----------
    def _display_name(self, path):
        if normcase(path) == normcase(self.fav_dir):
            return "我的收藏"
        return os.path.basename(path.rstrip(os.sep)) or path

    def _dir_children(self, path):
        """列出单个目录下的子文件夹与媒体文件，返回 (folders, files)。"""
        folders = []
        files = []
        try:
            entries = list(os.scandir(path))
        except OSError:
            return folders, files
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    name = e.name
                    if name.startswith(".") or name in SKIP_DIRS or name.startswith("@"):
                        continue
                    info = self._folder_stat(e.path)
                    if info:
                        folders.append(info)
                elif e.is_file():
                    k = kind_of(e.name)
                    if not k:
                        continue
                    st = e.stat()
                    it = {"path": e.path, "name": e.name, "kind": k, "ext": ext_of(e.name),
                          "size": st.st_size, "mtime": int(st.st_mtime), "rnd": 0}
                    files.append(self.serialize(it, width=320))
            except OSError:
                continue
        folders.sort(key=lambda x: x["name"].lower())
        files.sort(key=lambda x: x["name"].lower())
        return folders, files

    def folder_list(self, path):
        """返回当前目录的子文件夹与媒体文件。path 为空表示虚拟根。"""
        if not path:
            # 根目录不展示授权路径这一层，直接展开每个授权路径内部的子文件夹/文件，
            # 例如授权 /vol2/1000/片 → 直接看到「片」里面的子文件夹。
            folders = []
            files = []
            seen_dir = set()
            seen_file = set()
            for r in self.media_roots:
                fs, fls = self._dir_children(r)
                for f in fs:
                    key = normcase(f["path"])
                    if key not in seen_dir:
                        seen_dir.add(key)
                        folders.append(f)
                for f in fls:
                    key = normcase(f["path"])
                    if key not in seen_file:
                        seen_file.add(key)
                        files.append(f)
            folders.sort(key=lambda x: x["name"].lower())
            files.sort(key=lambda x: x["name"].lower())
            # 收藏目录仍作为独立入口保留在根目录
            if os.path.isdir(self.fav_dir):
                info = self._folder_stat(self.fav_dir)
                if info:
                    info.update({"root": True, "favorites": True})
                    folders.insert(0, info)
            return {"path": "", "name": "相册", "crumbs": [], "folders": folders, "files": files}

        path = os.path.abspath(path)
        folders, files = self._dir_children(path)
        return {
            "path": path,
            "name": self._display_name(path),
            "crumbs": self._crumbs(path),
            "folders": folders,
            "files": files,
        }

    def _crumbs(self, path):
        crumbs = []
        best = None
        for r in self.all_roots():
            if within(path, r) and (best is None or len(r) > len(best)):
                best = r
        if not best:
            return crumbs
        root_name = self._display_name(best)
        crumbs.append({"name": root_name, "path": best})
        rel = os.path.relpath(path, best)
        if rel and rel != ".":
            cur = best
            for part in rel.split(os.sep):
                cur = os.path.join(cur, part)
                crumbs.append({"name": part, "path": cur})
        return crumbs

    def _folder_stat(self, path):
        """文件夹封面 + 数量：从索引里用二分查找，避免重复遍历磁盘。"""
        count = 0
        cover = None
        cover_kind = None
        for r in self.all_roots():
            if not within(path, r):
                continue
            try:
                ent = self.get_index(r)
            except Exception:
                continue
            prefix = path.rstrip(os.sep) + os.sep
            lo = bisect.bisect_left(ent["paths"], prefix)
            hi = bisect.bisect_left(ent["paths"], prefix + "\U0010ffff")
            count += hi - lo
            if cover is None:
                for it in ent["items"][lo:min(hi, lo + 200)]:
                    if it["kind"] == "image":
                        cover, cover_kind = it, "image"
                        break
                if cover is None:
                    for it in ent["items"][lo:min(hi, lo + 200)]:
                        cover, cover_kind = it, it["kind"]
                        break
        try:
            mtime = int(os.stat(path).st_mtime)
        except OSError:
            mtime = 0
        name = self._display_name(path)
        data = {
            "name": name,
            "path": path,
            "count": count,
            "mtime": mtime,
            "cover": None,
            "coverKind": None,
        }
        if cover:
            data["cover"] = "/api/thumb?path=%s&w=320&v=%d" % (quote(cover["path"]), cover["mtime"])
            data["coverKind"] = cover_kind
        return data

    # ---------- 详情 ----------
    def detail(self, path):
        st = os.stat(path)
        name = os.path.basename(path)
        k = kind_of(name) or "file"
        w, h = (0, 0)
        if k == "image":
            cache = self.dims.get(path)
            w, h = cache if cache else (image_dims(path, ext_of(name)) or (0, 0))
        elif k == "video":
            m = self.vmeta.get(path) or {}
            w, h = m.get("w") or 0, m.get("h") or 0
            if not w:
                self.probe_video(path)
        exif = read_exif(path, ext_of(name))
        data = {
            "path": path,
            "name": name,
            "dir": os.path.dirname(path),
            "kind": k,
            "ext": ext_of(name),
            "size": st.st_size,
            "sizeText": human_size(st.st_size),
            "mtime": int(st.st_mtime),
            "mtimeText": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "ctimeText": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_ctime)),
            "w": w, "h": h,
            "fav": bool(self.fav_src.get(path)),
            "exif": fmt_exif(exif),
        }
        if k == "video":
            m = self.vmeta.get(path) or {}
            if m.get("duration"):
                data["duration"] = m["duration"]
        return data

    # ---------- 收藏 / 删除 ----------
    def add_favorite(self, src):
        src = os.path.realpath(src)
        if not self.is_allowed(src) or not os.path.isfile(src):
            raise ValueError("文件不可访问")
        with self.guard:
            exist = self.fav_src.get(src)
        if exist and os.path.exists(exist):
            return {"ok": True, "fav": exist, "already": True}
        if within(src, self.fav_dir):
            # 已经在收藏夹里了
            with self.guard:
                self.fav_src[src] = src
                self._save_fav()
            return {"ok": True, "fav": src, "already": True}

        base = os.path.basename(src)
        stem, ext = os.path.splitext(base)
        target = os.path.join(self.fav_dir, base)
        n = 1
        while os.path.exists(target):
            try:
                if os.path.samefile(target, src):
                    break
            except OSError:
                pass
            n += 1
            target = os.path.join(self.fav_dir, "%s (%d)%s" % (stem, n, ext))

        if not os.path.exists(target):
            ok = False
            try:
                os.link(src, target)
                ok = True
            except OSError:
                pass
            if not ok:
                try:
                    os.symlink(src, target)
                    ok = True
                except OSError:
                    pass
            if not ok:
                shutil.copy2(src, target)

        with self.guard:
            self.fav_map[target] = {"src": src, "name": base, "ts": int(time.time())}
            self.fav_src[src] = target
            self._save_fav()
            self.index.pop(self.fav_dir, None)
        return {"ok": True, "fav": target, "already": False}

    def remove_favorite(self, src):
        src = os.path.realpath(src)
        with self.guard:
            target = self.fav_src.pop(src, None)
            if target:
                self.fav_map.pop(target, None)
            self._save_fav()
            self.index.pop(self.fav_dir, None)
        # 只删除收藏夹里的那个目录项（硬链接 / 符号链接 / 副本），原始文件不受影响
        if target and target != src and within(target, self.fav_dir):
            try:
                if os.path.exists(target) or os.path.islink(target):
                    os.remove(target)
            except OSError:
                pass
        return {"ok": True}

    def delete(self, path):
        path = os.path.realpath(path)
        if not self.is_allowed(path):
            raise ValueError("文件不在允许访问的范围内")
        if not os.path.isfile(path):
            raise ValueError("文件不存在")
        if not kind_of(os.path.basename(path)):
            raise ValueError("只允许删除照片或视频文件")

        # 若是收藏夹里的条目，先把映射清理掉
        fav_copy = None
        with self.guard:
            src = self.fav_map.get(path, {}).get("src")
            if src:
                self.fav_map.pop(path, None)
                self.fav_src.pop(src, None)
                self._save_fav()
            else:
                fav_of = self.fav_src.pop(path, None)
                if fav_of:
                    self.fav_map.pop(fav_of, None)
                    self._save_fav()
                    fav_copy = fav_of

        os.remove(path)

        # 原始文件被删掉后，收藏夹里的硬链接/副本一并清理，避免留下孤儿文件
        if fav_copy and fav_copy != path and within(fav_copy, self.fav_dir):
            try:
                if os.path.exists(fav_copy):
                    os.remove(fav_copy)
            except OSError:
                pass

        with self.guard:
            self.dims.pop(path, None)
            self.vmeta.pop(path, None)
            for r in list(self.index.keys()):
                self.index.pop(r, None)
        return {"ok": True, "path": path}


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fnPhotowall/1.0"

    # 由 main() 注入
    library = None
    thumbs = None
    static_dir = None
    base_prefix = ""
    settings_file = ""
    data_dir = ""
    share_paths = []
    preloader = None
    started_at = time.time()

    def _cache_dirs(self):
        return cache_candidates(os.path.join(self.data_dir, "thumbs"),
                                self.library.media_roots if self.library else [],
                                self.share_paths,
                                self.thumbs.base if self.thumbs else "")

    # ---------------- 基础输出 ----------------
    def log_message(self, fmt, *args):
        if DEBUG:
            log("%s - %s" % (self.address_string(), fmt % args))

    def _resolve_path(self):
        raw = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(raw.path)
        prefix = self.base_prefix
        if prefix and path.startswith(prefix):
            path = path[len(prefix):] or "/"
        if not path.startswith("/"):
            path = "/" + path
        query = urllib.parse.parse_qs(raw.query, keep_blank_values=True)
        return path, query

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8",
              headers=None, cache=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = dict(headers or {})
        # 250 张一页的 JSON 约 60~80KB，gzip 后只剩十几 KB，首屏更快
        if (len(body) > 1024
                and "gzip" in (self.headers.get("Accept-Encoding") or "")
                and "Content-Encoding" not in headers
                and ctype.startswith(("application/json", "text/"))):
            try:
                import gzip
                body = gzip.compress(body, compresslevel=6)
                headers["Content-Encoding"] = "gzip"
                headers["Vary"] = "Accept-Encoding"
            except Exception:
                pass
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        else:
            self.send_header("Cache-Control", "no-store")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code=200, cache=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), cache=cache)

    def _fail(self, code, msg):
        self._json({"ok": False, "error": msg}, code=code)

    def _qp(self, query, key, default=None):
        v = query.get(key)
        return v[0] if v else default

    def _target(self, query):
        p = self._qp(query, "path")
        if not p:
            raise ValueError("缺少 path 参数")
        if "\x00" in p:
            raise ValueError("非法路径")
        p = os.path.realpath(p)
        if not self.library.is_allowed(p):
            raise PermissionError("该路径不在授权目录内")
        return p

    # ---------------- 路由 ----------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            path, query = self._resolve_path()
            if path.startswith("/api/"):
                self._api_get(path, query)
            else:
                self._static(path)
        except PermissionError as e:
            self._fail(403, str(e))
        except ValueError as e:
            self._fail(400, str(e))
        except FileNotFoundError:
            self._fail(404, "文件不存在")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("GET error: %s\n%s" % (e, traceback.format_exc()))
            self._fail(500, "服务内部错误")

    def do_POST(self):
        try:
            path, _query = self._resolve_path()
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            self._api_post(path, payload)
        except PermissionError as e:
            self._fail(403, str(e))
        except ValueError as e:
            self._fail(400, str(e))
        except Exception as e:
            log("POST error: %s\n%s" % (e, traceback.format_exc()))
            self._fail(500, "服务内部错误")

    # ---------------- API ----------------
    def _api_get(self, path, query):
        lib = self.library

        if path == "/api/health":
            return self._json({"ok": True, "uptime": int(time.time() - self.started_at),
                               "pil": PIL_OK, "ffmpeg": bool(FFMPEG),
                               "pageSize": PAGE_SIZE,
                               "thumbs": self.thumbs.stats(),
                               "roots": lib.media_roots})

        if path == "/api/preload":
            return self._json({"ok": True, "status": self.preloader.status()})

        if path == "/api/cache":
            # 查看缓存用量；?prune=1 立即执行一次清理
            if self._qp(query, "prune") == "1":
                result = self.thumbs.prune()
                return self._json({"ok": True, "prune": result, "stats": self.thumbs.stats()})
            return self._json({"ok": True, "stats": self.thumbs.stats()})

        if path == "/api/settings":
            return self._json({
                "ok": True,
                "settings": load_settings(self.settings_file),
                "cache": self.thumbs.stats(),
                "cacheDirs": self._cache_dirs(),
            })

        if path == "/api/roots":
            roots = []
            for r in lib.media_roots:
                roots.append({"name": os.path.basename(r) or r, "path": r, "favorites": False})
            if os.path.isdir(lib.fav_dir):
                roots.append({"name": "我的收藏", "path": lib.fav_dir, "favorites": True})
            return self._json({"roots": roots, "favDir": lib.fav_dir})

        if path == "/api/media":
            kinds = self._qp(query, "kind", "image")
            if kinds not in ("image", "video", "all"):
                kinds = "image"
            try:
                seed = int(self._qp(query, "seed", "0"))
            except ValueError:
                seed = 0
            try:
                offset = max(0, int(self._qp(query, "offset", "0")))
            except ValueError:
                offset = 0
            try:
                limit = int(self._qp(query, "limit", str(PAGE_SIZE)))
            except ValueError:
                limit = PAGE_SIZE
            # limit<=0 表示不限量（放开 250 张的限制）
            limit = 0 if limit <= 0 else min(20000, limit)
            sub = self._qp(query, "dir")
            if sub:
                sub = os.path.realpath(sub)
                if not lib.is_allowed(sub):
                    return self._fail(403, "该路径不在授权目录内")
            if self._qp(query, "refresh") == "1":
                lib.invalidate_if_stale(20)
            return self._json(lib.list_media(kinds, seed, offset, limit, sub))

        if path == "/api/folder":
            p = self._qp(query, "path") or ""
            if p:
                p = os.path.realpath(p)
                if not lib.is_allowed(p):
                    return self._fail(403, "该路径不在授权目录内")
                if not os.path.isdir(p):
                    return self._fail(404, "目录不存在")
            if self._qp(query, "refresh") == "1":
                lib.invalidate_if_stale(20)
            return self._json(lib.folder_list(p))

        if path == "/api/detail":
            p = self._target(query)
            if not os.path.isfile(p):
                return self._fail(404, "文件不存在")
            return self._json(lib.detail(p))

        if path == "/api/thumb":
            p = self._target(query)
            try:
                w = int(self._qp(query, "w", str(DEFAULT_THUMB)))
            except ValueError:
                w = DEFAULT_THUMB
            nearest = min(THUMB_WIDTHS, key=lambda x: abs(x - w))
            kind = kind_of(os.path.basename(p)) or "image"
            out = self.thumbs.get(p, nearest, kind)
            if out and os.path.exists(out):
                return self._send_file(out, "image/jpeg",
                                       cache="public, max-age=604800, immutable")
            # 降级：直接给原图（浏览器自行缩放）
            ext = ext_of(p)
            if kind == "image" and ext in MIME:
                return self._send_file(p, MIME[ext], cache="public, max-age=3600")
            return self._fail(404, "无法生成缩略图")

        if path == "/api/file":
            p = self._target(query)
            if not os.path.isfile(p):
                return self._fail(404, "文件不存在")
            ext = ext_of(p)
            ctype = MIME.get(ext, "application/octet-stream")
            return self._send_file(p, ctype, cache="private, max-age=86400",
                                   attachment=self._qp(query, "download") == "1",
                                   inline=self._qp(query, "inline") == "1")

        return self._fail(404, "未知接口")

    def _api_post(self, path, payload):
        lib = self.library

        # 设置项不针对具体文件，先处理
        if path == "/api/settings":
            data = load_settings(self.settings_file)

            if "cache_dir" in payload:
                target = payload.get("cache_dir") or ""
                if not target:
                    return self._fail(400, "缺少缓存目录")
                allowed = {normcase(c["path"]): c for c in self._cache_dirs()}
                hit = allowed.get(normcase(os.path.abspath(target)))
                if not hit:
                    return self._fail(400, "只能选择列表中的缓存目录")
                if not hit.get("writable"):
                    return self._fail(400, "该目录没有写入权限")
                res = self.thumbs.switch_base(hit["path"])
                data["cache_dir"] = res["base"]
                save_settings(self.settings_file, data)
                return self._json({
                    "ok": True, "settings": data,
                    "cache": res.get("stats") or self.thumbs.stats(),
                    "cacheDirs": self._cache_dirs(),
                })

            if "cache_limit_gb" in payload:
                try:
                    gb = float(payload.get("cache_limit_gb"))
                except (TypeError, ValueError):
                    return self._fail(400, "缓存上限格式不正确")
                if gb < 0.5 or gb > 200:
                    return self._fail(400, "缓存上限需在 0.5 ~ 200 GB 之间")
                res = self.thumbs.set_limit_gb(gb)
                data["cache_limit_gb"] = res["limitGb"]
                save_settings(self.settings_file, data)
                return self._json({
                    "ok": True, "settings": data,
                    "cache": res["stats"],
                    "cacheDirs": self._cache_dirs(),
                })

            save_settings(self.settings_file, data)
            return self._json({
                "ok": True, "settings": data,
                "cache": self.thumbs.stats(),
                "cacheDirs": self._cache_dirs(),
            })

        if path == "/api/preload":
            action = payload.get("action") or "status"
            if action == "start":
                started = self.preloader.start()
                return self._json({"ok": True, "started": started,
                                   "status": self.preloader.status()})
            if action == "stop":
                self.preloader.stop("user")
                return self._json({"ok": True, "status": self.preloader.status()})
            return self._json({"ok": True, "status": self.preloader.status()})

        if path == "/api/cache":
            if payload.get("prune") is True:
                result = self.thumbs.prune()
                return self._json({"ok": True, "prune": result, "stats": self.thumbs.stats()})
            return self._json({"ok": True, "stats": self.thumbs.stats()})

        p = payload.get("path")
        if not p:
            return self._fail(400, "缺少 path 参数")
        if "\x00" in p:
            return self._fail(400, "非法路径")
        real = os.path.realpath(p)
        if not lib.is_allowed(real):
            return self._fail(403, "该路径不在授权目录内")

        if path == "/api/favorite":
            action = payload.get("action") or "add"
            if action == "add":
                return self._json(lib.add_favorite(real))
            return self._json(lib.remove_favorite(real))

        if path == "/api/delete":
            if payload.get("confirm") is not True:
                return self._fail(400, "缺少二次确认")
            return self._json(lib.delete(real))

        return self._fail(404, "未知接口")

    # ---------------- 文件输出（支持 Range，视频拖动进度依赖它） ----------------
    def _send_file(self, full, ctype, cache=None, attachment=False, inline=False):
        try:
            size = os.path.getsize(full)
        except OSError:
            return self._fail(404, "文件不存在")

        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if range_header and range_header.startswith("bytes="):
            spec = range_header[6:].split(",")[0].strip()
            if "-" in spec:
                a, b = spec.split("-", 1)
                try:
                    if a == "":
                        start = max(0, size - int(b))
                        end = size - 1
                    else:
                        start = int(a)
                        end = int(b) if b else size - 1
                except ValueError:
                    start, end = 0, size - 1
                else:
                    if start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    end = min(end, size - 1)
                    partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if cache:
            self.send_header("Cache-Control", cache)
        else:
            self.send_header("Cache-Control", "no-store")
        if attachment:
            fn = urllib.parse.quote(os.path.basename(full))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % fn)
        elif inline:
            fn = urllib.parse.quote(os.path.basename(full))
            self.send_header("Content-Disposition", "inline; filename*=UTF-8''%s" % fn)
        self.end_headers()

        if self.command == "HEAD":
            return
        # 视频流畅度的关键：优先用 sendfile 零拷贝，避免 Python 层反复 read/write
        try:
            self.wfile.flush()
        except Exception:
            pass
        sent = False
        if not DEBUG_NO_SENDFILE and hasattr(self.connection, "sendfile"):
            try:
                with open(full, "rb") as f:
                    self.connection.sendfile(f, start, length)
                sent = True
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return
            except Exception:
                sent = False     # 某些文件系统/平台不支持，回退到普通写
        if sent:
            return
        try:
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1048576, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------------- 静态资源 ----------------
    def _static(self, path):
        if path in ("/", "", "/index.html"):
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        if ".." in rel.split("/"):
            return self._fail(400, "非法路径")
        full = os.path.realpath(os.path.join(self.static_dir, rel))
        if not within(full, self.static_dir) or not os.path.isfile(full):
            # 单页应用回退
            full = os.path.join(self.static_dir, "index.html")
            rel = "index.html"
            if not os.path.isfile(full):
                return self._fail(404, "资源不存在")

        ext = os.path.splitext(full)[1].lower()
        ctype = STATIC_MIME.get(ext, "application/octet-stream")

        if rel in ("index.html", "ultra.html"):
            with open(full, "r", encoding="utf-8") as f:
                html = f.read()
            base = (self.base_prefix or "") + "/"
            html = html.replace("{{BASE}}", base).replace("{{APPNAME}}", "瀑布相册")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return

        cache = "public, max-age=86400" if ext in (".png", ".jpg", ".webp", ".woff2", ".svg") else "no-cache"
        return self._send_file(full, ctype, cache=cache)


# --------------------------------------------------------------------------
# 服务器
# --------------------------------------------------------------------------

class ThreadingHTTPServer6(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if DEBUG:
            log("handler error: %s" % traceback.format_exc())


if hasattr(socket, "AF_UNIX"):
    class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        address_family = socket.AF_UNIX
        daemon_threads = True
        allow_reuse_address = True
        request_queue_size = 64

        def server_bind(self):
            try:
                os.unlink(self.server_address)
            except OSError:
                pass
            self.socket.bind(self.server_address)
            self.server_name = "fnphotowall"
            self.server_port = 0

        def get_request(self):
            req, _ = self.socket.accept()
            return req, ("unix", 0)

        def handle_error(self, request, client_address):
            if DEBUG:
                log("handler error: %s" % traceback.format_exc())
else:  # Windows 调试环境没有 AF_UNIX
    UnixHTTPServer = None


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def split_paths(value):
    if not value:
        return []
    out = []
    for p in re.split(r"[:;]", value):
        p = p.strip()
        if p and os.path.isdir(p):
            out.append(os.path.abspath(p))
    return out


def load_settings(path):
    """应用内的用户设置（缓存上限等），存在 TRIM_PKGVAR/settings.json。"""
    default = {}
    if not path:
        return default
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return default


def save_settings(path, data):
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:
        log("save settings failed: %s" % e)


def disk_free(path):
    """返回 (free, total)，路径不存在时向上找最近的已存在目录。"""
    p = os.path.abspath(path)
    for _ in range(6):
        try:
            du = shutil.disk_usage(p)
            return du.free, du.total
        except OSError:
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
    return 0, 0


def cache_candidates(default_base, media_roots, share_paths, current_base):
    """可供选择的缓存目录：应用数据目录 / 共享目录 / 各授权目录下的隐藏目录。"""
    items = []
    seen = set()

    def add(path, label, kind, hint):
        key = normcase(path)
        if key in seen:
            return
        seen.add(key)
        free, total = disk_free(path)
        parent = path if os.path.isdir(path) else (os.path.dirname(path) or path)
        writable = os.path.isdir(parent) and os.access(parent, os.W_OK)
        items.append({
            "path": path,
            "label": label,
            "hint": hint,
            "kind": kind,
            "free": free,
            "total": total,
            "freeText": human_size(free) if free else "",
            "writable": bool(writable),
            "current": normcase(path) == normcase(current_base),
        })

    add(default_base, "应用数据目录", "default", "随应用安装位置，默认选项")
    for p in share_paths:
        add(os.path.join(p, "thumb-cache"), "应用共享目录", "share", "在文件管理里可见")
    for r in media_roots:
        name = os.path.basename(r.rstrip(os.sep)) or r
        add(os.path.join(r, ".fnphotowall_cache"), "%s 所在磁盘" % name, "root",
            "隐藏在相册目录内，不会被扫描")
    return items


def load_config(etc):
    if not etc:
        return {}
    try:
        cfg_path = os.path.join(etc, "config.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def gather_roots(args, cfg=None):
    cfg = cfg or {}
    roots = []
    roots += [os.path.abspath(p) for p in (args.root or [])]
    # 用户授权目录（应用设置里授权后由飞牛注入）
    roots += split_paths(os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS"))
    if not roots:
        roots += split_paths(os.environ.get("PHOTOWALL_ROOTS"))
    # 配置文件里也可以写死/补充
    for p in cfg.get("roots") or []:
        if os.path.isdir(p):
            roots.append(os.path.abspath(p))

    seen = set()
    uniq = []
    for r in roots:
        if not os.path.isdir(r):
            continue
        key = normcase(r)
        if key in seen:
            continue
        # 去掉被其它根目录包含的子目录，避免重复展示
        if any(within(r, u) and normcase(r) != normcase(u) for u in uniq):
            continue
        uniq = [u for u in uniq if not within(u, r)]
        seen = {normcase(u) for u in uniq}
        seen.add(key)
        uniq.append(r)
    return uniq


def gather_fav_dir(args):
    if args.fav:
        return os.path.abspath(args.fav)
    for p in split_paths(os.environ.get("TRIM_DATA_SHARE_PATHS")):
        return os.path.join(p, "收藏")
    return os.path.join(args.data, "favorites")


def main():
    ap = argparse.ArgumentParser(description="fnPhotowall backend")
    ap.add_argument("--socket", help="Unix Socket 路径（统一网关）")
    ap.add_argument("--port", type=int, default=0, help="调试用 TCP 端口")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--static", required=True, help="前端静态目录")
    ap.add_argument("--data", required=True, help="运行数据目录")
    ap.add_argument("--etc", default="", help="配置目录")
    ap.add_argument("--log", default="", help="日志文件")
    ap.add_argument("--root", action="append", help="调试：指定相册根目录，可多次")
    ap.add_argument("--fav", help="收藏夹目录")
    ap.add_argument("--appname", default="fnphotowall")
    ap.add_argument("--prefix", default=None, help="网关前缀，默认 /app/<appname>")
    ap.add_argument("--thumb-gb", type=float, default=0,
                    help="缩略图缓存上限（GB），默认 10")
    ap.add_argument("--thumb-keep", type=int, default=0,
                    help="缩略图保底张数，默认 5000（≈20 屏 × 250 张）")
    ap.add_argument("--thumb-workers", type=int, default=0,
                    help="缩略图生成并发线程数，默认按 CPU 自动")
    args = ap.parse_args()

    if args.log:
        try:
            os.makedirs(os.path.dirname(args.log), exist_ok=True)
            fh = open(args.log, "a", encoding="utf-8", buffering=1)
            sys.stderr = fh
        except Exception:
            pass

    os.makedirs(args.data, exist_ok=True)
    cfg = load_config(args.etc)
    user_settings = load_settings(os.path.join(args.data, "settings.json"))
    roots = gather_roots(args, cfg)
    fav_dir = gather_fav_dir(args)

    try:
        thumb_gb = float(args.thumb_gb or user_settings.get("cache_limit_gb")
                         or cfg.get("thumb_cache_gb")
                         or os.environ.get("PHOTOWALL_THUMB_GB") or 10)
    except (TypeError, ValueError):
        thumb_gb = 10
    try:
        thumb_keep = int(args.thumb_keep or cfg.get("thumb_keep")
                         or os.environ.get("PHOTOWALL_THUMB_KEEP") or THUMB_MIN_KEEP)
    except (TypeError, ValueError):
        thumb_keep = THUMB_MIN_KEEP
    try:
        thumb_workers = int(args.thumb_workers or cfg.get("thumb_workers") or 0)
    except (TypeError, ValueError):
        thumb_workers = 0

    log("=" * 60)
    log("fnPhotowall starting | python=%s | PIL=%s | ffmpeg=%s" %
        (sys.version.split()[0], PIL_OK, bool(FFMPEG)))
    log("roots = %s" % roots)
    log("favorites = %s" % fav_dir)
    log("env TRIM_DATA_ACCESSIBLE_PATHS = %r" % os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS"))

    lib = Library(roots, fav_dir, args.data)
    default_thumb_dir = os.path.join(args.data, "thumbs")
    thumb_dir = default_thumb_dir
    saved_dir = user_settings.get("cache_dir")
    if saved_dir and os.path.isdir(os.path.dirname(os.path.abspath(saved_dir))):
        thumb_dir = os.path.abspath(saved_dir)
        log("thumb cache dir from settings: %s" % thumb_dir)
    thumbs = ThumbNails(thumb_dir,
                        workers=thumb_workers,
                        cache_limit=int(thumb_gb * 1024 ** 3),
                        min_keep=thumb_keep)
    lib.thumbs = thumbs

    Handler.library = lib
    Handler.thumbs = thumbs
    Handler.static_dir = os.path.realpath(args.static)
    Handler.base_prefix = args.prefix if args.prefix is not None else "/app/%s" % args.appname
    Handler.settings_file = os.path.join(args.data, "settings.json")
    Handler.data_dir = os.path.abspath(args.data)
    Handler.preloader = Preloader(lib, thumbs)
    Handler.share_paths = split_paths(os.environ.get("TRIM_DATA_SHARE_PATHS"))
    Handler.started_at = time.time()

    thumbs.prune_async()     # 启动时先做一次轻量巡检，不阻塞首屏
    lib.warmup()

    if args.socket:
        srv = UnixHTTPServer(args.socket, Handler)
        try:
            os.chmod(args.socket, 0o666)
        except OSError:
            pass
        log("listening on unix socket %s (prefix=%s)" % (args.socket, Handler.base_prefix))
    else:
        port = args.port or 8787
        srv = ThreadingHTTPServer6((args.host, port), Handler)
        log("listening on http://%s:%d (prefix=%s)" % (args.host, port, Handler.base_prefix))

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if args.socket and os.path.exists(args.socket):
            try:
                os.unlink(args.socket)
            except OSError:
                pass
        try:
            thumbs.shutdown()
        except Exception:
            pass
        log("stopped")


if __name__ == "__main__":
    main()
