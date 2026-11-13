# /// script
# requires-python = ">=3.10"
# dependencies = ["cramjam==2.11.0", "dfindexeddb==20260210"]
# [tool.uv]
# override-dependencies = ["python-snappy==0.7.3"]
# ///
"""转发即下载：监视微信视频号详情缓存，发现新作品时自动下载。

触发条件：微信把频道卡片的详情写进 `feed_cache_info`（转发到任意会话即会触发），
本脚本只在对应 LevelDB 日志 mtime 变化时才重新解析，避免空转。

用法：
    uv run sharewatch.py --out ~/Movies/wechat-channels
    uv run sharewatch.py --once --dry-run
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import re
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# GUI 应用从 Finder/launchd 启动时 PATH 只有系统目录，补齐 Homebrew 路径。
for _extra in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _extra not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _extra + ":" + os.environ.get("PATH", "")

# 分发版（PyInstaller 单文件）：ffmpeg/ffprobe 与 worker 同放在 Resources/bin，
# 把该目录加进 PATH，下载引擎的 shutil.which 才能找到校验工具。
if getattr(sys, "frozen", False):
    _bundled_bin = Path(sys.executable).resolve().parent
    if _bundled_bin not in [Path(p) for p in os.environ.get("PATH", "").split(":")]:
        os.environ["PATH"] = str(_bundled_bin) + ":" + os.environ.get("PATH", "")

# 下载引擎（channels*.py）的查找顺序：
# 1. PyInstaller 打包资源目录（分发版 app 内嵌）
# 2. 仓库内 wcdl_engine/（源码运行 / uv run）
# 3. 旧 codex skill 目录（历史开发机兜底，正式分发不依赖）
import importlib.resources  # noqa: E402

_ENGINE_CANDIDATES: list[Path] = []
if getattr(sys, "_MEIPASS", None):  # PyInstaller 解包目录
    _ENGINE_CANDIDATES.append(Path(sys._MEIPASS) / "wcdl_engine")
_ENGINE_CANDIDATES.append(Path(__file__).resolve().parent / "wcdl_engine")
_ENGINE_CANDIDATES.append(
    Path.home() / ".codex/skills/wechat-channels-download/scripts")
for _engine_dir in _ENGINE_CANDIDATES:
    if (_engine_dir / "channels.py").is_file():
        sys.path.insert(0, str(_engine_dir))
        break
else:
    raise SystemExit(
        "找不到下载引擎 channels.py；期待位置：打包资源、wcdl_engine/、"
        "~/.codex/skills/wechat-channels-download/scripts")

import channels  # noqa: E402
import channels_preload  # noqa: E402
import channels_storage  # noqa: E402

PROFILES = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles"

# 宿主 app 每 5 秒把这个文件重写成自己的 pid；worker 据此判断自己是不是孤儿。
# 只靠信号不可行：app 被 SIGTERM/SIGKILL 杀掉时，uv 和 python 不会被连带回收。
HEARTBEAT = Path.home() / ".channels-watch/worker.heartbeat"


EVENTS = False

PROGRESS_RE = re.compile(r"Downloaded ([\d.]+) / ([\d.]+) MB \(([\d.]+)%\)")


class ProgressStream:
    """把 channels.download 的 \r 进度行转成 JSONL 事件。"""

    def __init__(self, real):
        self.real = real
        self.buf = ""
        self.last = -1.0

    def write(self, chunk):
        self.real.write(chunk)
        self.buf += chunk
        for done, total, pct in PROGRESS_RE.findall(self.buf):
            value = float(pct)
            if value - self.last >= 1.0:
                self.last = value
                emit("progress", done=float(done), total=float(total), percent=value)
        if len(self.buf) > 4096:
            self.buf = self.buf[-1024:]

    def flush(self):
        self.real.flush()


def log(message: str) -> None:
    """带时间戳输出到 stderr，便于 launchd 收集。"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def emit(kind: str, **fields) -> None:
    """给宿主的 JSONL 事件；未开启 --events 时静默。"""
    if EVENTS:
        payload = {"type": kind, "ts": time.time(), **fields}
        print(json.dumps(payload, ensure_ascii=False), flush=True)


def session_feeds() -> list:
    """读取 Session Storage 里的 cache::feed-obj_<id>（收藏/点赞/进详情会写）。"""
    feeds = []
    for profile in PROFILES.iterdir():
        storage = profile / "Session Storage"
        if not storage.is_dir():
            continue
        latest = {}
        for path in sorted(storage.iterdir()):
            if path.suffix not in (".log", ".ldb", ".sst"):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            try:
                entries = (list(channels_storage.table_entries(data)) if path.suffix != ".log"
                           else [e for b in channels_storage.log_records(data)
                                 for e in channels_storage.batch_entries(b)])
            except Exception:
                continue
            for entry in entries:
                if entry.key not in latest or entry.sequence > latest[entry.key][0].sequence:
                    latest[entry.key] = (entry, path)
        for key, (entry, src) in latest.items():
            name = key.decode("latin-1")
            if "cache::feed-obj_" not in name or entry.deleted:
                continue
            feed = channels_storage.decode_feed(entry.value, storage, entry.sequence)
            if feed is None:
                continue
            feed.metadata_source = "session"
            # 绝不用「当前时刻」伪造时间戳：那会让 --max-age 彻底失效，
            # 账本一清就把 Session Storage 里的历史条目批量误下。
            # 用承载它的日志文件 mtime 作为写入时间的兜底。
            try:
                observed = int(src.stat().st_mtime * 1000)
            except OSError:
                observed = 0
            # 收藏时间以日志写入时间为准：视频的发布时间可能早几小时甚至几天，
            # 若沿用它，--max-age 会把刚收藏的老视频全部跳过。
            feed.updated = observed
            feed.provenance = [{"name": "session", "state": "active",
                                "observed_at_ms": observed, "path": str(src),
                                "record_sequence": entry.sequence}]
            feeds.append(feed)
    return feeds


def blob_feeds() -> list:
    """解析 IndexedDB 外置 blob 里的 feed_cache_info。

    同一个库可能有多个 shard 目录（`1/03/3a7`、`d/00/9`…），写入时只替换其中
    一个并删掉同目录旧文件，所以必须**全量遍历**而不是只看 mtime 最新的那个。
    """
    import cramjam

    picked: dict = {}  # fid -> (createTime, inner, 来源文件)
    for blob_dir in PROFILES.glob("*/IndexedDB/*.indexeddb.blob"):
        for path in blob_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            raw = data
            if data[:3] == b"\xff\x11\x02":
                try:
                    raw = bytes(cramjam.snappy.decompress_raw(data[3:]))
                except Exception:
                    continue
            start = raw.find(b"{\x00")
            if start < 0:
                continue
            try:
                payload = json.loads(raw[start:].decode("utf-16-le", "ignore").rstrip("\x00"))
            except (ValueError, UnicodeError):
                continue
            if not isinstance(payload, dict):
                continue
            for fid, wrapper in payload.items():
                if not str(fid).isdigit() or not isinstance(wrapper, dict):
                    continue
                inner = (wrapper.get("data") or {}).get("data", {}).get("object")
                if not isinstance(inner, dict):
                    continue
                created = wrapper.get("createTime") or wrapper.get("ts") or 0
                seen = picked.get(str(fid))
                if seen is not None and seen[0] >= created:
                    continue
                picked[str(fid)] = (created, inner, path)

    feeds = []
    for fid, (created, inner, path) in picked.items():
        feed = channels_storage.decode_feed(
            json.dumps(inner, ensure_ascii=False).encode(), path, 0)
        if feed is None:
            continue
        feed.updated = created
        feed.metadata_source = "blob"
        feed.provenance = [{"name": "blob", "state": "active", "observed_at_ms": created,
                           "path": str(path), "record_sequence": 0}]
        feeds.append(feed)
    return feeds


def log_files() -> list[Path]:
    """返回用于变更检测的日志：IndexedDB 与 Session Storage 都要看。

    收藏只写 Session Storage，若只盯 IndexedDB，点心后不会唤醒 worker。
    """
    paths = list(PROFILES.glob("*/IndexedDB/weixin_xworker_0.indexeddb.leveldb/*.log"))
    paths += list(PROFILES.glob("*/Session Storage/*.log"))
    return sorted(paths)


def signature(paths: list[Path]) -> tuple:
    """(路径, mtime, 大小) 指纹；任何写入都会改变它。"""
    result = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        result.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(result)


def load_state(path: Path) -> dict:
    """读取已处理账本；损坏时退回空账本而不是崩溃。"""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"seen": {}}


def save_state(path: Path, state: dict) -> None:
    """原子写回账本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def prune_state(state: dict, keep_days: float) -> int:
    """账本只增不减会越读写越慢，按天数淘汰旧记录。"""
    if keep_days <= 0:
        return 0
    cutoff = time.time() - keep_days * 86400
    before = len(state["seen"])
    state["seen"] = {k: v for k, v in state["seen"].items() if v.get("at", 0) >= cutoff}
    return before - len(state["seen"])


def acquire_lock(path: Path):
    """同一份账本只允许一个 worker 写，避免多实例重复下载、互相覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = open(path, "w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_handle.close()
        return None
    return lock_handle


def orphaned() -> bool:
    """宿主 app 没了就意味着自己成了孤儿，必须退出，否则会一直抢账本。

    心跳文件里存的是 app 的 pid：pid 消失立即判定（3 秒级），pid 还在但
    60 秒没刷新（app 卡死）也判定；没有心跳文件说明是手动运行，不做约束。
    """
    try:
        raw = HEARTBEAT.read_text().strip()
    except OSError:
        return False
    if not raw.isdigit():
        return False
    try:
        os.kill(int(raw), 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OverflowError):
        return False
    try:
        return time.time() - HEARTBEAT.stat().st_mtime > 60
    except OSError:
        return False


def age_seconds(feed) -> float:
    """记录写入距今的秒数；没有时间戳时视为很旧。"""
    stamps = [s.get("observed_at_ms") for s in getattr(feed, "provenance", []) if s.get("observed_at_ms")]
    if not stamps:
        return float("inf")
    return time.time() - max(stamps) / 1000.0


def notify(text: str) -> None:
    """系统通知；失败只记日志，不影响下载结果。"""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification {json.dumps(text)} with title "视频号下载"'],
            check=False, timeout=10, capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        log("通知发送失败（忽略）")


def handle(feed, args, state) -> None:
    """下载单个作品并记账。"""
    title = f"{feed.nickname}_{feed.description[:30]}"
    age = age_seconds(feed)
    # age_seconds 在拿不到时间戳时返回 inf，而 round(inf) 会抛 OverflowError。
    age_label = "写入时间未知" if math.isinf(age) else f"{age:.0f}s 前写入"
    log(f"发现新作品 {feed.id} {title} ({age_label})")
    emit("detected", id=feed.id, title=feed.description[:80], nickname=feed.nickname,
         duration=feed.media.get("videoPlayLen"),
         age=None if math.isinf(age) else round(age))
    if args.dry_run:
        state["seen"][feed.id] = {"at": time.time(), "result": "dry-run"}
        return
    emit("downloading", id=feed.id, title=feed.description[:80])
    try:
        with contextlib.redirect_stderr(ProgressStream(sys.stderr)):
            result = channels.download(
                feed, Path(args.out).expanduser(), preference=args.format,
                mode=args.verify, retries=3, overwrite=False, progress=True,
            )
    except Exception as exc:  # 下载失败必须记下来，不能静默
        log(f"下载失败 {feed.id}: {type(exc).__name__}: {exc}")
        emit("failed", id=feed.id, error=f"{type(exc).__name__}: {exc}"[:200])
        state["seen"][feed.id] = {"at": time.time(), "result": "failed", "error": str(exc)[:200]}
        return
    size_mb = result["bytes"] / 1e6
    log(f"完成 {result['path']} ({size_mb:.1f} MB, {result['metadata'].get('validation')})")
    emit("done", id=feed.id, path=result["path"], bytes=result["bytes"],
         validation=result["metadata"].get("validation"), title=feed.description[:80],
         nickname=feed.nickname)
    state["seen"][feed.id] = {
        "at": time.time(), "result": "ok", "path": result["path"],
        "bytes": result["bytes"], "validation": result["metadata"].get("validation"),
    }
    if args.notify:
        notify(f"{feed.nickname} {size_mb:.1f}MB 已下载")


# --- 本地 HTTP 接口：供 DSH 左侧栏插件读取状态/控制 ---
_HTTP_STATE: dict = {"paused": False}
# 触发源集合放模块级：HTTP 线程运行中改写，扫描循环每轮读最新值（热更新）
_TRIGGERS: set = set()


def start_http(state: dict, state_path: Path, out_dir: Path) -> None:
    """在 127.0.0.1 上提供 /status /pause /resume /open /reveal。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port = int(os.environ.get("CHANNELS_WATCH_HTTP_PORT", "47821"))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 静默
            return

        def _send(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def do_GET(self):
            if self.path.split("?")[0] != "/status":
                self._send({"error": "not found"}, 404)
                return
            seen = state.get("seen", {})
            items = sorted(seen.items(), key=lambda kv: kv[1].get("at", 0), reverse=True)[:10]
            now = time.time()
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            today = sum(1 for _, m in seen.items() if m.get("at", 0) >= midnight)
            week = sum(1 for _, m in seen.items() if m.get("at", 0) >= midnight - 6 * 86400)
            self._send({
                "paused": _HTTP_STATE["paused"],
                "total": len(seen),
                "today": today,
                "week": week,
                "out": str(out_dir),
                "recent": [
                    {"id": fid, "path": m.get("path", ""), "bytes": m.get("bytes", 0),
                     "at": m.get("at", 0), "validation": m.get("validation", "")}
                    for fid, m in items
                ],
            })

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            path = self.path.split("?")[0]
            if path == "/pause":
                _HTTP_STATE["paused"] = True
                self._send({"ok": True, "paused": True})
            elif path == "/resume":
                _HTTP_STATE["paused"] = False
                self._send({"ok": True, "paused": False})
            elif path == "/triggers":
                # 热更新触发源：宿主 app 切开关时无需重启 worker（避免监视空窗）
                requested = body.get("triggers")
                if not isinstance(requested, list):
                    self._send({"ok": False, "error": "triggers must be a list"}, 400)
                    return
                allowed = {"fav", "forward"}
                _TRIGGERS.clear()
                _TRIGGERS.update(t for t in requested if t in allowed)
                log(f"触发源热更新：{'/'.join(sorted(_TRIGGERS)) or '无'}")
                self._send({"ok": True, "triggers": sorted(_TRIGGERS)})
            elif path == "/open":
                subprocess.Popen(["open", str(out_dir)])
                self._send({"ok": True})
            elif path == "/reveal":
                target = body.get("path") or ""
                if target and Path(target).exists():
                    subprocess.Popen(["open", "-R", target])
                    self._send({"ok": True})
                else:
                    self._send({"ok": False, "error": "missing"}, 400)
            else:
                self._send({"error": "not found"}, 404)

    def serve():
        try:
            ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
        except OSError as exc:
            log(f"HTTP 接口启动失败：{exc}")

    threading.Thread(target=serve, daemon=True, name="channels-http").start()
    log(f"HTTP 接口 http://127.0.0.1:{port}/status")


def main() -> int:
    """命令行入口：单次扫描或常驻轮询。"""
    ap = argparse.ArgumentParser(description="转发即下载：监视视频号详情缓存")
    ap.add_argument("--out", default=str(Path.home() / "Movies/wechat-channels"))
    ap.add_argument("--format", default="best-h264", help="best-h264 / best-h265 / best / 规格代码")
    ap.add_argument("--verify", default="full", choices=("full", "metadata", "none"))
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--max-age", type=float, default=600, help="只处理最近 N 秒内写入的记录")
    ap.add_argument("--state", default=str(Path.home() / ".channels-watch/state.json"))
    ap.add_argument("--keep-days", type=float, default=180,
                    help="账本保留天数；<=0 表示不清理")
    ap.add_argument("--triggers", default="fav,forward",
                    help="启用的触发源，逗号分隔：fav=收藏/点赞，forward=转发")
    ap.add_argument("--notify", action="store_true", help="下载完成后发系统通知")
    ap.add_argument("--once", action="store_true", help="只扫描一次")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不下载")
    ap.add_argument("--events", action="store_true", help="向 stdout 输出 JSONL 事件，供宿主 app 消费")
    ap.add_argument("--watch-parent", action="store_true",
                    help="常驻模式下监视宿主心跳，心跳中断就退出（供菜单栏 app 使用）")
    args = ap.parse_args()
    global EVENTS
    EVENTS = args.events
    _TRIGGERS.clear()
    _TRIGGERS.update(part.strip() for part in args.triggers.split(",") if part.strip())

    paths = log_files()
    if not paths:
        log("找不到 IndexedDB 日志，确认微信已登录且播放/转发过视频号作品")
        return 1
    state_path = Path(args.state)
    lock = acquire_lock(state_path.with_suffix(".lock"))
    if lock is None:
        log(f"另一个 worker 正在使用 {state_path}，本实例退出")
        # 75 = EX_TEMPFAIL：宿主 app 靠这个退出码区分「锁冲突（等待即可）」
        # 和「真崩溃（需要重启）」，用 0 会让 app 陷入重启风暴。
        return 75
    state = load_state(state_path)
    dropped = prune_state(state, args.keep_days)
    if dropped:
        save_state(state_path, state)
        log(f"账本淘汰了 {dropped} 条超过 {args.keep_days:g} 天的记录")
    log(f"监视 {len(paths)} 个日志文件，账本 {state_path}，已记录 {len(state['seen'])} 条")
    try:
        start_http(state, state_path, Path(args.out).expanduser())
    except Exception as exc:  # 接口失败不能影响下载
        log(f"HTTP 接口异常：{type(exc).__name__}: {exc}")

    try:
        known_blob = {f.id for f in blob_feeds()}  # 启动时的存量，不算新增
    except Exception:
        known_blob = set()
    last: tuple = ()  # 首轮强制扫描，避免启动前已写入的记录被漏掉
    while True:
        # 每轮重算：LevelDB 会轮转日志（000021 → 000025），缓存旧列表会漏掉新文件
        current = signature(log_files())
        if _HTTP_STATE["paused"]:
            time.sleep(args.interval)
            continue
        if current != last or args.once:
            last = current
            try:
                # 触发一：Session Storage 的 cache::feed-obj_<id>（收藏/点赞）。
                feeds = session_feeds() if "fav" in _TRIGGERS else []
            except Exception as exc:
                log(f"扫描失败：{type(exc).__name__}: {exc}")
                feeds = []

            # 触发二：详情缓存的“单条新增”（手机/电脑转发会写一条）。
            # 滚动列表会在一次扫描里新增多条，这里按数量区分，避免批量下载。
            try:
                current_blob = {f.id: f for f in blob_feeds()}
                fresh_blob = set(current_blob) - known_blob
                if "forward" in _TRIGGERS and len(fresh_blob) == 1:
                    feeds.append(current_blob[next(iter(fresh_blob))])
                    log(f"转发触发：+1 条 {next(iter(fresh_blob))}")
                elif "forward" in _TRIGGERS and len(fresh_blob) > 1:
                    log(f"跳过批量新增 {len(fresh_blob)} 条（疑似滚动浏览）")
                # 即使关掉转发源也要跟进存量，避免重新打开时把历史条目当新增
                known_blob |= set(current_blob)
            except Exception as exc:
                log(f"转发源解析失败：{type(exc).__name__}: {exc}")
            fresh = [f for f in feeds if f.id not in state["seen"]
                     and age_seconds(f) < args.max_age]
            for feed in fresh:
                handle(feed, args, state)
                save_state(state_path, state)
        if args.once:
            break
        if args.watch_parent and orphaned():
            log("宿主 app 心跳中断，worker 退出")
            return 0
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
