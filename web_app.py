"""switched.cloud 定时签到与任务控制台（本地回环 127.0.0.1，免登录，端口 50914）。

启动：
    .venv/bin/python -B -u web_app.py
安全说明：
    本服务无密码鉴权，强制仅监听 127.0.0.1；如需远程访问请使用 SSH 端口转发。
"""
from __future__ import annotations

import copy
import json
import os
import random
import re
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from switched_core import (  # noqa: E402
    ProviderError,
    SessionExpiredError,
    SwitchedClient,
)

CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"
COOKIE_GUIDE_PATH = ROOT / "assets" / "cookie_guide.png"
SNAP_CACHE_PATH = ROOT / ".snap_cache.json"
BIND_HOST = "127.0.0.1"
MAX_LOGS = 400

CFG_LOCK = threading.RLock()
STATE_LOCK = threading.RLock()

STATE: dict = {
    "running": False,   # 唯一总开关状态
    "refreshing": False,
    "phase": "idle",    # idle / working / cooling / done
    "action": "",
    "next_try_at": 0.0,
    "session_ok": None,
    "session_msg": "尚未检测",
    "snap": None,
    "fails": {},        # provider -> {"n": int, "until": epoch_float, "day": "YYYY-MM-DD"}
    "logs": [],
    "log_seq": 0,
    "last_result": "",
}
STOP_EVENT = threading.Event()
_VPS_CACHE: list[str] | None = None


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_line(msg: str) -> None:
    ts = datetime.now()
    line = f"[{ts:%H:%M:%S}] {msg}"
    with STATE_LOCK:
        STATE["logs"].append(line)
        del STATE["logs"][:-MAX_LOGS]
        STATE["log_seq"] += 1
    print(f"[{ts:%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _default_cfg() -> dict:
    return {
        "server": {"host": BIND_HOST, "port": 50914},
        "panel": {
            "origin": "https://panel.switched.cloud",
            "cookie": "",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "timeout": 30,
        },
        "tasks": {
            "streak": True,
            "adfocus": True,
            "linkvertise": True,
            "gap_min": 25,
            "gap_max": 60,
            "lv_auth": "",
            "lv_ut": "",
        },
        "keepalive": {
            "session_enabled": True,
            "session_interval_min": 10,
            "server_enabled": False,
            "server_interval_sec": 60,
        },
        "schedule": {
            "enabled": False,
            "time": "08:30",
            "jitter_min": 30,
            "last_run_date": "",
            "last_streak_date": "",
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_cfg() -> dict:
    with CFG_LOCK:
        raw = {}
        if CONFIG_PATH.exists():
            try:
                raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                print(f"[cfg] 读取 config.yaml 失败：{exc}", file=sys.stderr, flush=True)
        cfg = _deep_merge(_default_cfg(), raw if isinstance(raw, dict) else {})
        cfg["server"]["host"] = BIND_HOST
        return cfg


def save_cfg(cfg: dict) -> None:
    with CFG_LOCK:
        cfg.setdefault("server", {})["host"] = BIND_HOST
        atomic_write(CONFIG_PATH, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))


def _load_snap_cache() -> dict | None:
    if not SNAP_CACHE_PATH.is_file():
        return None
    try:
        data = json.loads(SNAP_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("providers") is not None else None
    except Exception:
        return None


def _save_snap_cache(snap: dict) -> None:
    try:
        atomic_write(SNAP_CACHE_PATH, json.dumps(snap, ensure_ascii=False))
    except Exception:
        pass


def _set_snap(snap: dict, msg: str | None = None) -> None:
    with STATE_LOCK:
        STATE["snap"] = snap
        STATE["session_ok"] = True
        STATE["session_msg"] = msg or f"正常（{snap.get('checked_at', '')}）"
    _save_snap_cache(snap)


def _bootstrap() -> None:
    if not CONFIG_PATH.exists() and EXAMPLE_PATH.exists():
        shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
    with CFG_LOCK:
        if not CONFIG_PATH.exists():
            save_cfg(_default_cfg())
    cached = _load_snap_cache()
    if cached:
        with STATE_LOCK:
            STATE["snap"] = cached
            STATE["session_msg"] = f"缓存（{cached.get('checked_at', '未知')}）"


_bootstrap()


def _persist_cookie(new_cookie: str) -> None:
    with CFG_LOCK:
        cfg = load_cfg()
        if cfg["panel"].get("cookie") != new_cookie:
            cfg["panel"]["cookie"] = new_cookie
            save_cfg(cfg)


def make_client(cfg: dict | None = None) -> SwitchedClient:
    c = cfg or load_cfg()
    p = c["panel"]
    return SwitchedClient(
        origin=p.get("origin") or "https://panel.switched.cloud",
        cookie_str=p.get("cookie") or "",
        user_agent=p.get("user_agent") or "",
        timeout=int(p.get("timeout") or 30),
        on_cookie_update=_persist_cookie,
    )


def do_refresh_snap() -> dict:
    with STATE_LOCK:
        STATE["refreshing"] = True
    try:
        client = make_client()
        snap = client.snapshot()
        _set_snap(snap)
        return {"ok": True, "snap": snap}
    except SessionExpiredError as exc:
        with STATE_LOCK:
            STATE["session_ok"] = False
            STATE["session_msg"] = str(exc)
        log_line(f"[session] {exc}")
        return {"ok": False, "info": str(exc)}
    except Exception as exc:
        log_line(f"[snap] 刷新失败：{exc}")
        return {"ok": False, "info": str(exc)}
    finally:
        with STATE_LOCK:
            STATE["refreshing"] = False


def trigger_refresh_async() -> dict:
    cfg = load_cfg()
    if not cfg["panel"].get("cookie"):
        return {"ok": False, "refreshing": False, "info": "尚未配置 Cookie"}
    with STATE_LOCK:
        if STATE["refreshing"]:
            return {"ok": True, "refreshing": True, "info": "正在后台刷新中"}
        STATE["refreshing"] = True

    def _bg() -> None:
        do_refresh_snap()

    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "refreshing": True, "info": "已触发后台刷新"}


def _today_str() -> str:
    return datetime.now().date().isoformat()


def _reset_stale_fails() -> None:
    today = _today_str()
    with STATE_LOCK:
        stale = [k for k, v in STATE["fails"].items() if v.get("day") and v.get("day") != today]
        for k in stale:
            STATE["fails"].pop(k, None)


def _bump_fail(provider: str, reason: str, cool_s: int = 0) -> None:
    today = _today_str()
    with STATE_LOCK:
        f = STATE["fails"].get(provider) or {"n": 0, "until": 0.0, "day": today}
        if f.get("day") != today:
            f = {"n": 0, "until": 0.0, "day": today}
        f["n"] = int(f.get("n", 0)) + 1
        wait = cool_s if cool_s > 0 else (3600 if f["n"] >= 3 else (120 if f["n"] == 1 else 300))
        f["until"] = time.time() + wait
        f["day"] = today
        STATE["fails"][provider] = f
    till = datetime.fromtimestamp(f["until"]).strftime("%H:%M")
    log_line(f"[{provider}] 失败({f['n']})：{reason} → 冷却 {wait}s（预计 {till} 重试）")


def _clear_fail(provider: str) -> None:
    with STATE_LOCK:
        STATE["fails"][provider] = {"n": 0, "until": 0.0, "day": _today_str()}


def _fail_until(provider: str) -> float:
    today = _today_str()
    with STATE_LOCK:
        f = STATE["fails"].get(provider) or {}
    if f.get("day") and f.get("day") != today:
        return 0.0
    return float(f.get("until") or 0.0)


def _enabled_providers(snap: dict, tasks_cfg: dict) -> list[dict]:
    return [
        p for p in (snap.get("providers") or [])
        if p.get("supported") and tasks_cfg.get(p["provider"], False)
    ]


def _pick_next_provider(snap: dict, tasks_cfg: dict) -> dict | None:
    now = time.time()
    cands = []
    for p in _enabled_providers(snap, tasks_cfg):
        if p.get("left", 0) <= 0:
            continue
        if _fail_until(p["provider"]) > now:
            continue
        cands.append(p)
    cands.sort(key=lambda x: float(x.get("quote") or 0), reverse=True)
    return cands[0] if cands else None


def _all_tasks_done(snap: dict, tasks_cfg: dict) -> bool:
    streak_pending = bool(tasks_cfg.get("streak", True) and (snap.get("streak") or {}).get("claimable"))
    if streak_pending:
        return False
    enabled = _enabled_providers(snap, tasks_cfg)
    if not enabled:
        return True
    return all(int(p.get("left") or 0) <= 0 for p in enabled)


def start_task(mode: str = "all", source: str = "manual") -> dict:
    with STATE_LOCK:
        if STATE["running"]:
            return {"ok": False, "info": "总开关已处于开启状态"}
        STATE["running"] = True
        STATE["phase"] = "working"
        STATE["action"] = f"启动（{source}）"
        STATE["next_try_at"] = 0.0
        STOP_EVENT.clear()

    def worker() -> None:
        done_count = 0
        auto_closed = False
        try:
            log_line(f"[master] 总开关已打开（来源={source}）")
            while not STOP_EVENT.is_set():
                _reset_stale_fails()
                cfg = load_cfg()
                tasks_cfg = cfg["tasks"]
                client = make_client(cfg)

                # 1. 拉取面板快照（网络瞬时抖动不关总开关，30s 后重试）
                try:
                    snap = client.snapshot()
                except SessionExpiredError:
                    raise
                except Exception as exc:
                    log_line(f"[snap] 获取面板失败：{exc}，30s 后重试")
                    with STATE_LOCK:
                        STATE["phase"] = "cooling"
                        STATE["action"] = "网络异常，30s 后重试"
                        STATE["next_try_at"] = time.time() + 30
                    if STOP_EVENT.wait(30):
                        break
                    continue

                _set_snap(snap)

                # 2. 自动领取每日连签
                if tasks_cfg.get("streak", True):
                    st = snap.get("streak") or {}
                    if st.get("claimable"):
                        with STATE_LOCK:
                            STATE["phase"] = "working"
                            STATE["action"] = "每日连签"
                            STATE["next_try_at"] = 0.0
                        try:
                            res = client.claim_streak(snap)
                            log_line(f"[streak] {res['info']}")
                            with CFG_LOCK:
                                c2 = load_cfg()
                                c2["schedule"]["last_streak_date"] = _today_str()
                                save_cfg(c2)
                            snap = client.snapshot()
                            _set_snap(snap)
                        except SessionExpiredError:
                            raise
                        except Exception as exc:
                            log_line(f"[streak] 连签异常：{exc}")

                if STOP_EVENT.is_set():
                    break

                # 3. 监控所有启用项目是否已全部完成 → 自动关闭总开关
                if _all_tasks_done(snap, tasks_cfg):
                    auto_closed = True
                    log_line(f"[master] 所有启用项目今日已全部完成（本次入账 {done_count} 次），总开关自动关闭")
                    with STATE_LOCK:
                        STATE["phase"] = "done"
                        STATE["action"] = "今日已全部完成"
                        STATE["last_result"] = f"{now_str()} 今日已全部完成（入账 {done_count} 次），总开关已自动关闭"
                    break

                # 4. 挑选可用渠道；若未满额渠道全在冷却，保持总开关开启并睡到最早到期时间
                prov = _pick_next_provider(snap, tasks_cfg)
                if not prov:
                    now_ts = time.time()
                    unfull = [p for p in _enabled_providers(snap, tasks_cfg) if int(p.get("left") or 0) > 0]
                    untils = [_fail_until(p["provider"]) for p in unfull]
                    active_untils = [u for u in untils if u > now_ts]
                    if active_untils:
                        target_ts = min(active_untils) + 3.0
                        wait_s = max(5.0, target_ts - now_ts)
                    else:
                        wait_s = 30.0
                        target_ts = now_ts + wait_s
                    till_str = datetime.fromtimestamp(target_ts).strftime("%H:%M:%S")
                    names = "、".join(f"{p['provider']}({p.get('granted_today')}/{p.get('daily_cap')})" for p in unfull)
                    log_line(f"[master] 未完成渠道 [{names}] 均在冷却中，保持总开关开启，预计 {till_str} 自动继续")
                    with STATE_LOCK:
                        STATE["phase"] = "cooling"
                        STATE["action"] = f"冷却等待至 {till_str[:5]}"
                        STATE["next_try_at"] = target_ts
                    if STOP_EVENT.wait(wait_s):
                        break
                    continue

                # 5. 执行选中的免验证渠道
                key = prov["provider"]
                before = int(prov.get("granted_today") or 0)
                with STATE_LOCK:
                    STATE["phase"] = "working"
                    STATE["action"] = f"{key}（{before}/{prov.get('daily_cap')}）"
                    STATE["next_try_at"] = 0.0
                log_line(f"[run] 执行 {key}（今日 {before}/{prov.get('daily_cap')}）")
                quick_next = False
                try:
                    if key == "adfocus":
                        ok = client.run_adfocus(log=log_line, stop_event=STOP_EVENT)
                    elif key == "linkvertise":
                        ok = client.run_linkvertise(
                            auth=str(tasks_cfg.get("lv_auth") or "").strip(),
                            ut=str(tasks_cfg.get("lv_ut") or "").strip(),
                            log=log_line,
                            stop_event=STOP_EVENT,
                        )
                    else:
                        ok = False
                    if STOP_EVENT.is_set():
                        break
                    snap = client.snapshot()
                    _set_snap(snap)
                    after_row = next((x for x in snap["providers"] if x["provider"] == key), None)
                    after = int(after_row.get("granted_today") or 0) if after_row else before
                    if ok and after > before:
                        _clear_fail(key)
                        done_count += 1
                        log_line(f"[{key}] 到账成功（今日 {after}/{after_row.get('daily_cap')}）")
                    else:
                        _bump_fail(key, "面板计数未增加")
                        quick_next = True
                except ProviderError as exc:
                    _bump_fail(key, str(exc), cool_s=exc.cool_s)
                    quick_next = True
                except SessionExpiredError:
                    raise
                except Exception as exc:
                    _bump_fail(key, str(exc))
                    quick_next = True

                if STOP_EVENT.is_set():
                    break

                if _all_tasks_done(snap, tasks_cfg):
                    continue
                if not _pick_next_provider(snap, tasks_cfg):
                    continue

                gap_min = max(5, int(tasks_cfg.get("gap_min") or 25))
                gap_max = max(gap_min, int(tasks_cfg.get("gap_max") or 60))
                wait_s = random.randint(3, 5) if quick_next else random.randint(gap_min, gap_max)
                target_ts = time.time() + wait_s
                with STATE_LOCK:
                    STATE["phase"] = "working"
                    STATE["action"] = f"等待 {wait_s}s 下一轮"
                    STATE["next_try_at"] = target_ts
                log_line(f"[run] 等待 {wait_s}s 进入下一轮…")
                if STOP_EVENT.wait(wait_s):
                    break

            if STOP_EVENT.is_set() and not auto_closed:
                log_line(f"[master] 总开关已手动关闭（本次入账 {done_count} 次）")
                with STATE_LOCK:
                    STATE["last_result"] = f"{now_str()} 已手动关闭（本次入账 {done_count} 次）"
        except SessionExpiredError as exc:
            with STATE_LOCK:
                STATE["session_ok"] = False
                STATE["session_msg"] = str(exc)
                STATE["last_result"] = f"{now_str()} 会话失效，总开关已关闭"
            log_line(f"[session] {exc}，总开关已自动关闭")
        except Exception as exc:
            log_line(f"[master] 异常：{exc}\n{traceback.format_exc()}")
            with STATE_LOCK:
                STATE["last_result"] = f"{now_str()} 异常关闭：{exc}"
        finally:
            with STATE_LOCK:
                STATE["running"] = False
                STATE["next_try_at"] = 0.0
                if not auto_closed:
                    STATE["phase"] = "idle"
                    STATE["action"] = ""

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "info": "总开关已打开"}


def stop_task() -> dict:
    with STATE_LOCK:
        if not STATE["running"]:
            return {"ok": False, "info": "总开关当前已关闭"}
    STOP_EVENT.set()
    log_line("[master] 正在关闭总开关…")
    return {"ok": True, "info": "已发送关闭信号"}


# ---------------------------------------------------------------- 定时调度 & 保活
def sched_target_for_day(day, hhmm: str, jitter_min: int) -> tuple[datetime, int]:
    hh, mm = (int(x) for x in hhmm.split(":"))
    base = datetime(day.year, day.month, day.day, hh, mm)
    j = max(0, min(120, int(jitter_min or 0)))
    off = random.Random(f"sw|{day.isoformat()}|{hhmm}").randint(-j, j) if j > 0 else 0
    return base + timedelta(minutes=off), off


def next_sched_text(cfg: dict) -> str:
    sc = cfg.get("schedule") or {}
    if not sc.get("enabled"):
        return "未启用"
    hhmm = str(sc.get("time") or "08:30")
    if not re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
        return "时间格式无效"
    now = datetime.now()
    for delta in (0, 1, 2):
        day = (now + timedelta(days=delta)).date()
        t, off = sched_target_for_day(day, hhmm, int(sc.get("jitter_min") or 0))
        if day.isoformat() == sc.get("last_run_date"):
            continue
        if t > now:
            left = t - now
            h, m = int(left.total_seconds() // 3600), int(left.total_seconds() % 3600 // 60)
            return f"{t:%m-%d %H:%M}（基准 {hhmm}，随机 {off:+d}m，剩 {h}h{m:02d}m）"
    return "待计算"


def sched_loop() -> None:
    while True:
        try:
            time.sleep(max(1, 60 - datetime.now().second) + 1)
            cfg = load_cfg()
            sc = cfg.get("schedule") or {}
            if not sc.get("enabled"):
                continue
            hhmm = str(sc.get("time") or "08:30")
            if not re.fullmatch(r"\d{1,2}:\d{2}", hhmm):
                continue
            now = datetime.now().replace(second=0, microsecond=0)
            today_str = now.date().isoformat()
            if sc.get("last_run_date") == today_str:
                continue
            target, off = sched_target_for_day(now.date(), hhmm, int(sc.get("jitter_min") or 0))
            if target <= now <= target + timedelta(minutes=30):
                with CFG_LOCK:
                    fresh = load_cfg()
                    fresh["schedule"]["last_run_date"] = today_str
                    save_cfg(fresh)
                with STATE_LOCK:
                    busy = STATE["running"]
                if busy:
                    log_line(f"[sched] 到达每日定时（基准 {hhmm}，随机 {off:+d}m），总开关已处于开启状态")
                else:
                    log_line(f"[sched] 定时触发：自动打开总开关（基准 {hhmm}，随机 {off:+d}m）")
                    start_task(mode="all", source="schedule")
        except Exception as exc:
            log_line(f"[sched] 调度异常：{exc}")
            time.sleep(30)


def keepalive_loop() -> None:
    global _VPS_CACHE
    last_sess = 0.0
    last_srv = 0.0
    while True:
        try:
            time.sleep(15)
            cfg = load_cfg()
            if not cfg["panel"].get("cookie"):
                continue
            ka = cfg.get("keepalive") or {}
            now_m = time.monotonic()
            if ka.get("session_enabled"):
                iv = max(2, int(ka.get("session_interval_min") or 10)) * 60
                if now_m - last_sess >= iv:
                    last_sess = now_m
                    try:
                        make_client(cfg).ping_session()
                        with STATE_LOCK:
                            STATE["session_ok"] = True
                            STATE["session_msg"] = f"保活正常（{datetime.now():%H:%M:%S}）"
                    except SessionExpiredError as exc:
                        with STATE_LOCK:
                            STATE["session_ok"] = False
                            STATE["session_msg"] = str(exc)
                        log_line(f"[keepalive] {exc}")
                    except Exception as exc:
                        log_line(f"[keepalive] 会话保活异常：{exc}")

            if ka.get("server_enabled"):
                iv_s = max(20, int(ka.get("server_interval_sec") or 60))
                if now_m - last_srv >= iv_s:
                    last_srv = now_m
                    try:
                        cli = make_client(cfg)
                        if _VPS_CACHE is None:
                            _VPS_CACHE = cli.list_vps()
                            if not _VPS_CACHE:
                                log_line("[keepalive] 未找到 VPS UUID，跳过服务器保活")
                        if _VPS_CACHE:
                            cli.ping_vps(_VPS_CACHE)
                    except SessionExpiredError as exc:
                        with STATE_LOCK:
                            STATE["session_ok"] = False
                            STATE["session_msg"] = str(exc)
                        log_line(f"[keepalive] {exc}")
                    except Exception as exc:
                        log_line(f"[keepalive] VPS 保活异常：{exc}")
        except Exception as exc:
            log_line(f"[keepalive] 线程异常：{exc}")
            time.sleep(30)


# ---------------------------------------------------------------- Web API 处理
def safe_cfg_for_ui() -> dict:
    cfg = load_cfg()
    ck = cfg["panel"].get("cookie") or ""
    return {
        "panel": {
            "origin": cfg["panel"].get("origin"),
            "has_cookie": bool(ck),
            "cookie_len": len(ck),
        },
        "tasks": cfg["tasks"],
        "keepalive": cfg["keepalive"],
        "schedule": cfg["schedule"],
        "next_run": next_sched_text(cfg),
    }


def do_update_cfg(payload: dict) -> dict:
    global _VPS_CACHE
    with CFG_LOCK:
        cfg = load_cfg()
        if "cookie" in payload and str(payload.get("cookie") or "").strip():
            raw_ck = str(payload["cookie"]).strip()
            if "…" in raw_ck or "..." in raw_ck:
                return {"ok": False, "info": "Cookie 中包含省略号（被开发者工具截断），请在 Network 中切换「原始 / Raw」视图复制一整行"}
            cleaned = re.sub(r"^\s*cookie\s*:?\s*", "", raw_ck, flags=re.I)
            cleaned = re.sub(r"[\r\n\t]+", " ", cleaned).strip()
            cfg["panel"]["cookie"] = cleaned
            _VPS_CACHE = None
        if isinstance(payload.get("tasks"), dict):
            t = payload["tasks"]
            for k in ("streak", "adfocus", "linkvertise"):
                if k in t:
                    cfg["tasks"][k] = bool(t[k])
            for k in ("gap_min", "gap_max"):
                if t.get(k) not in (None, ""):
                    cfg["tasks"][k] = max(1, int(t[k]))
            cfg["tasks"]["gap_min"] = max(5, int(cfg["tasks"].get("gap_min") or 25))
            cfg["tasks"]["gap_max"] = max(cfg["tasks"]["gap_min"], int(cfg["tasks"].get("gap_max") or 60))
            for k in ("lv_auth", "lv_ut"):
                if k in t:
                    cfg["tasks"][k] = str(t[k] or "").strip()
        if isinstance(payload.get("keepalive"), dict):
            ka = payload["keepalive"]
            for k in ("session_enabled", "server_enabled"):
                if k in ka:
                    cfg["keepalive"][k] = bool(ka[k])
            if ka.get("session_interval_min") not in (None, ""):
                cfg["keepalive"]["session_interval_min"] = max(2, int(ka["session_interval_min"]))
            if ka.get("server_interval_sec") not in (None, ""):
                cfg["keepalive"]["server_interval_sec"] = max(20, int(ka["server_interval_sec"]))
        if isinstance(payload.get("schedule"), dict):
            sc = payload["schedule"]
            if "enabled" in sc:
                cfg["schedule"]["enabled"] = bool(sc["enabled"])
            if sc.get("time"):
                tstr = str(sc["time"]).strip()
                if not re.fullmatch(r"\d{1,2}:\d{2}", tstr):
                    return {"ok": False, "info": "定时时间格式应为 HH:MM"}
                hh, mm = (int(x) for x in tstr.split(":"))
                if not (0 <= hh < 24 and 0 <= mm < 60):
                    return {"ok": False, "info": "定时时间越界"}
                cfg["schedule"]["time"] = f"{hh:02d}:{mm:02d}"
            if sc.get("jitter_min") not in (None, ""):
                cfg["schedule"]["jitter_min"] = max(0, min(120, int(sc["jitter_min"])))
        save_cfg(cfg)
    log_line("[cfg] 配置已保存")
    return {"ok": True, "cfg": safe_cfg_for_ui(), "info": "配置已保存"}


def build_status() -> dict:
    cfg = load_cfg()
    now = time.time()
    today = _today_str()
    with STATE_LOCK:
        fails_view = {
            k: {
                "n": 0 if (v.get("day") and v.get("day") != today) else v.get("n", 0),
                "cool_left": 0 if (v.get("day") and v.get("day") != today) else max(0, int(v.get("until", 0) - now)),
            }
            for k, v in STATE["fails"].items()
        }
        return {
            "ok": True,
            "running": STATE["running"],
            "refreshing": STATE["refreshing"],
            "phase": STATE["phase"],
            "action": STATE["action"],
            "next_try_at": STATE["next_try_at"],
            "session_ok": STATE["session_ok"],
            "session_msg": STATE["session_msg"],
            "snap": STATE["snap"],
            "fails": fails_view,
            "logs": list(STATE["logs"]),
            "log_seq": STATE["log_seq"],
            "last_result": STATE["last_result"],
            "next_run": next_sched_text(cfg),
            "has_cookie": bool(cfg["panel"].get("cookie")),
        }


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>switched.cloud 定时签到控制台</title>
<style>
  :root{--bg:#08090b;--card:#1c1c1e;--line:#303034;--fg:#f5f5f7;--dim:#999ba4;--acc:#3698ff;--ok:#3fb950;--bad:#f85149}
  *{box-sizing:border-box}
  body{margin:0;padding:24px 16px 105px;background:var(--bg);color:var(--fg);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .wrap{max-width:480px;margin:0 auto}
  h1{font-size:22px;margin:0 0 4px}.sub{font-size:12px;color:var(--dim);margin-bottom:14px}
  .topbar{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:var(--card);border-radius:14px;margin-bottom:14px;font-size:12px}
  .hero{border-radius:24px;padding:24px;margin-bottom:16px;background:linear-gradient(125deg,#087fff,#347ffa 60%,#4fc5ec);color:#fff}
  .hero h2{font-size:28px;margin:8px 0}.hero p{margin:4px 0;opacity:.92;font-size:13px}
  .hero .btns{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
  .hero button{margin:0;padding:10px 16px;border-radius:10px;border:1px solid #ffffff38;background:#ffffff24;color:#fff;font-weight:600;cursor:pointer}
  .hero button.master-on{background:#c9372c;border-color:#ff7b7288}
  .hero button.master-off{background:#198754;border-color:#57d68f88}
  .card{background:var(--card);border-radius:22px;padding:22px;margin-bottom:16px}
  .card h2{font-size:16px;margin:0 0 12px}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .prow{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #2b2b30;font-size:13px}
  .prow:last-child{border-bottom:0}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;border:1px solid var(--line);color:var(--dim)}
  .pill.on{color:var(--ok);border-color:#2ea04366}.pill.bad{color:var(--bad);border-color:#f8514966}
  label{display:block;font-size:12px;color:var(--dim);margin:10px 0 4px}
  input,textarea{width:100%;padding:11px;background:#111114;border:1px solid #353539;border-radius:10px;color:var(--fg);font-size:13px}
  textarea#log{height:240px;font:12px/1.5 Consolas,monospace;white-space:pre;resize:vertical}
  .chk{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:13px}.chk input{width:auto}
  button{margin-top:12px;padding:11px 14px;border-radius:12px;border:1px solid var(--line);background:#303034;color:var(--fg);font-weight:600;cursor:pointer}
  button.primary{background:#0787ff;border-color:#0787ff;color:#fff}
  button:disabled{opacity:.45;cursor:not-allowed}
  .out{margin-top:10px;font-size:12px;min-height:18px;color:var(--ok);word-break:break-all}.out.bad{color:var(--bad)}
  .guide-img{width:100%;border-radius:10px;border:1px solid #353539;display:block;margin-top:8px}
  .guide-tip{font-size:13px;color:var(--fg);font-weight:600;margin-bottom:4px}
  .nav{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);display:flex;justify-content:space-around;
       width:min(400px,calc(100% - 32px));padding:8px;background:#202023ed;border:1px solid #37373c;border-radius:28px;backdrop-filter:blur(16px)}
  .nav button{margin:0;background:none;border:0;color:var(--dim);font-size:13px;padding:8px 16px}.nav button.active{color:#3698ff}
  .hide{display:none!important}
</style></head><body>
<div class="wrap">
  <h1>switched.cloud 控制台 <span id="rpill" class="pill">已关闭</span></h1>
  <div class="sub">定时连签 · 会话/VPS 保活 · 免验证渠道直刷（仅监听 127.0.0.1）</div>
  <div class="topbar">
    <span>会话：<b id="sess">检测中</b></span>
    <span class="pill">仅本地访问</span>
  </div>

  <div data-view="home">
    <div class="hero">
      <div>Cloud Tokens · 连签 <b id="streak">–</b></div>
      <h2 id="bal">–</h2>
      <p id="subbal">进度：– ｜ 下次定时：<span id="nextrun">–</span></p>
      <div class="btns">
        <button id="masterbtn" class="master-off" onclick="toggleMaster()">开启总开关</button>
        <button id="refbtn" onclick="refreshSnap()">刷新面板</button>
      </div>
    </div>
    <div class="card">
      <h2>渠道今日进度</h2>
      <div id="plist" style="color:var(--dim);font-size:13px">请先配置 Cookie 后点击「刷新面板」</div>
    </div>
    <div class="card">
      <h2>运行日志 <span id="lastres" style="font-size:11px;color:var(--dim);font-weight:400"></span></h2>
      <textarea id="log" readonly></textarea>
    </div>
  </div>

  <div data-view="sched" class="hide">
    <div class="card">
      <h2>每日定时打开总开关</h2>
      <div class="chk"><input id="sc_en" type="checkbox"><label for="sc_en" style="margin:0">启用每日定时自动打开总开关</label></div>
      <div class="row">
        <div><label>基准时间（HH:MM）</label><input id="sc_time" placeholder="08:30"></div>
        <div><label>随机偏移（±分钟）</label><input id="sc_jit" type="number" value="30"></div>
      </div>
      <h2 style="margin-top:20px">任务开关</h2>
      <div class="chk"><input id="t_streak" type="checkbox"><label for="t_streak" style="margin:0">自动领取每日连签（Daily streak）</label></div>
      <div class="chk"><input id="t_adfocus" type="checkbox"><label for="t_adfocus" style="margin:0">adfocus 纯 HTTP 直刷</label></div>
      <div class="chk"><input id="t_lv" type="checkbox"><label for="t_lv" style="margin:0">linkvertise GraphQL 直刷</label></div>
      <div class="row">
        <div><label>轮间隔最小（秒）</label><input id="t_gmin" type="number" value="25"></div>
        <div><label>轮间隔最大（秒）</label><input id="t_gmax" type="number" value="60"></div>
      </div>
      <h2 style="margin-top:20px">会话与 VPS 保活</h2>
      <div class="chk"><input id="ka_sess" type="checkbox"><label for="ka_sess" style="margin:0">定时 GET /tokens 保活登录态</label></div>
      <div class="row">
        <div><label>会话保活间隔（分钟）</label><input id="ka_smin" type="number" value="10"></div>
        <div><label>VPS 保活间隔（秒）</label><input id="ka_vsec" type="number" value="60"></div>
      </div>
      <div class="chk"><input id="ka_srv" type="checkbox"><label for="ka_srv" style="margin:0">定时 GET /vps/&lt;uuid&gt; 防服务器闲置</label></div>
      <button class="primary" style="width:100%" onclick="saveCfg()">保存定时与任务配置</button>
      <div id="cfgout" class="out"></div>
    </div>
  </div>

  <div data-view="cfg" class="hide">
    <div class="card">
      <h2>panel.switched.cloud Cookie</h2>
      <div class="sub" id="ckstat">未配置</div>
      <label>粘贴整行 Cookie（含 remember_web_* 更持久）</label>
      <textarea id="ckinput" rows="4" placeholder="Cookie: remember_web_...=...; switched_cloud_session=...; XSRF-TOKEN=..."></textarea>
      <button class="primary" style="width:100%" onclick="saveCookie()">保存 Cookie 并校验</button>
      <div id="ckout" class="out"></div>
    </div>
    <div class="card">
      <h2>Linkvertise 凭据（可选）</h2>
      <div class="sub">留空走匿名模式；匿名被风控时可填入浏览器采集的 Authorization / X-Linkvertise-UT</div>
      <label>lv_auth（Bearer Token，不含 Bearer 前缀）</label>
      <input id="lv_auth" placeholder="可选，留空为匿名">
      <label>lv_ut（X-Linkvertise-UT）</label>
      <input id="lv_ut" placeholder="可选，留空为匿名">
      <button style="width:100%" onclick="saveLvCreds()">保存 Linkvertise 凭据</button>
      <div id="lvout" class="out"></div>
    </div>
    <div class="card">
      <h2>Cookie 复制示例</h2>
      <div class="guide-tip">复制一整行就可以了，不用筛选</div>
      <div class="sub">打开浏览器 F12 → 网络（Network）→ 点开 panel.switched.cloud 任意请求 → 右上角切到「原始 / Raw」→ 复制整行 Cookie 粘贴到上方输入框即可。</div>
      <img class="guide-img" src="/assets/cookie_guide.png" alt="Cookie 复制示例">
    </div>
  </div>
</div>

<nav class="nav">
  <button class="active" data-tab="home" onclick="show('home')">概览</button>
  <button data-tab="sched" onclick="show('sched')">定时与任务</button>
  <button data-tab="cfg" onclick="show('cfg')">Cookie与设置</button>
</nav>
<script>
const $ = id => document.getElementById(id);
const out = (id, msg, bad) => { const n = $(id); n.textContent = msg; n.className = 'out' + (bad ? ' bad' : ''); };
async function api(path, body) {
  const r = await fetch(path, {method: body ? 'POST' : 'GET',
    headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return r.json();
}
function show(v) {
  document.querySelectorAll('[data-view]').forEach(el => el.classList.toggle('hide', el.dataset.view !== v));
  document.querySelectorAll('.nav button').forEach(b => b.classList.toggle('active', b.dataset.tab === v));
}
async function loadCfg() {
  const d = await api('/api/config');
  if (!d.ok) return false;
  const c = d.cfg;
  $('ckstat').textContent = c.panel.has_cookie ? ('已配置 Cookie（长度 ' + c.panel.cookie_len + '）') : '尚未配置 Cookie';
  $('sc_en').checked = !!c.schedule.enabled;
  $('sc_time').value = c.schedule.time || '08:30';
  $('sc_jit').value = c.schedule.jitter_min ?? 30;
  $('t_streak').checked = !!c.tasks.streak;
  $('t_adfocus').checked = !!c.tasks.adfocus;
  $('t_lv').checked = !!c.tasks.linkvertise;
  $('t_gmin').value = c.tasks.gap_min ?? 25;
  $('t_gmax').value = c.tasks.gap_max ?? 60;
  $('ka_sess').checked = !!c.keepalive.session_enabled;
  $('ka_smin').value = c.keepalive.session_interval_min ?? 10;
  $('ka_srv').checked = !!c.keepalive.server_enabled;
  $('ka_vsec').value = c.keepalive.server_interval_sec ?? 60;
  $('lv_auth').value = c.tasks.lv_auth || '';
  $('lv_ut').value = c.tasks.lv_ut || '';
  return !!c.panel.has_cookie;
}
async function saveLvCreds() {
  const r = await api('/api/config', {
    tasks: {lv_auth: $('lv_auth').value.trim(), lv_ut: $('lv_ut').value.trim()},
  });
  out('lvout', r.info || (r.ok ? '已保存' : '失败'), !r.ok);
}
async function saveCfg() {
  const r = await api('/api/config', {
    schedule: {enabled: $('sc_en').checked, time: $('sc_time').value.trim(), jitter_min: $('sc_jit').value},
    tasks: {streak: $('t_streak').checked, adfocus: $('t_adfocus').checked, linkvertise: $('t_lv').checked,
            gap_min: $('t_gmin').value, gap_max: $('t_gmax').value},
    keepalive: {session_enabled: $('ka_sess').checked, session_interval_min: $('ka_smin').value,
                server_enabled: $('ka_srv').checked, server_interval_sec: $('ka_vsec').value},
  });
  out('cfgout', r.info || (r.ok ? '已保存' : '失败'), !r.ok);
  if (r.ok) poll();
}
async function saveCookie() {
  const ck = $('ckinput').value.trim();
  if (!ck) { out('ckout', '请粘贴 Cookie', true); return; }
  out('ckout', '保存并校验中…');
  const r = await api('/api/config', {cookie: ck});
  if (!r.ok) { out('ckout', r.info || '保存失败', true); return; }
  $('ckinput').value = '';
  const s = await api('/api/refresh', {});
  out('ckout', s.ok ? 'Cookie 有效，已刷新面板快照' : ('已保存但校验失败：' + s.info), !s.ok);
  loadCfg(); poll();
}
function renderSnap(snap, fails, nextRun) {
  if (!snap) return;
  $('bal').textContent = (snap.balance && snap.balance.main) || '–';
  $('streak').textContent = (snap.streak && snap.streak.text) || '–';
  $('subbal').innerHTML = '进度：' + ((snap.balance && snap.balance.sub) || '–') + ' ｜ 下次定时：<span id="nextrun">' + (nextRun || '未启用') + '</span>';
  const rows = (snap.providers || []).map(p => {
    const f = (fails || {})[p.provider];
    const tag = !p.supported ? ('浏览器手动') : (p.full ? '今日已满' : (f && f.cool_left > 0 ? ('冷却 ' + f.cool_left + 's') : ('剩 ' + p.left)));
    const cls = !p.supported ? 'pill' : (p.full ? 'pill on' : (f && f.cool_left > 0 ? 'pill bad' : 'pill on'));
    return `<div class="prow"><span><b>${p.name}</b> <span style="color:var(--dim);font-size:12px">${p.granted_today}/${p.daily_cap}</span></span><span class="${cls}">${tag}</span></div>`;
  }).join('');
  $('plist').innerHTML = rows || '无渠道数据';
}
async function refreshSnap() {
  const rb = $('refbtn');
  if (rb) { rb.disabled = true; rb.textContent = '刷新中…'; }
  await api('/api/refresh', {async: true}).catch(() => {});
  poll();
}
let isMasterOn = false;
async function toggleMaster() {
  await api(isMasterOn ? '/api/stop' : '/api/run', isMasterOn ? {} : {mode: 'all'});
  poll();
}
let lastLogSeq = -1;
async function poll() {
  try {
    const d = await api('/api/status');
    isMasterOn = !!d.running;
    const mb = $('masterbtn');
    mb.textContent = isMasterOn ? '关闭总开关' : '开启总开关';
    mb.className = isMasterOn ? 'master-on' : 'master-off';
    const rb = $('refbtn');
    if (rb) { rb.disabled = !!d.refreshing; rb.textContent = d.refreshing ? '刷新中…' : '刷新面板'; }
    const phaseTxt = d.running
      ? (d.action ? ('开启 · ' + d.action) : '开启')
      : (d.phase === 'done' ? '今日已全部完成' : '已关闭');
    $('rpill').textContent = phaseTxt;
    $('rpill').className = 'pill' + (d.running ? (d.phase === 'cooling' ? ' bad' : ' on') : (d.phase === 'done' ? ' on' : ''));
    $('sess').textContent = d.session_msg || '未知';
    $('sess').style.color = d.session_ok === true ? 'var(--ok)' : (d.session_ok === false ? 'var(--bad)' : 'var(--dim)');
    $('nextrun').textContent = d.next_run || '未启用';
    $('lastres').textContent = d.last_result || '';
    if (d.snap) {
      renderSnap(d.snap, d.fails, d.next_run);
      try { localStorage.setItem('sw_snap_cache', JSON.stringify(d.snap)); } catch (e) {}
    }
    const seq = d.log_seq ?? (d.logs || []).length;
    if (seq !== lastLogSeq) { $('log').value = (d.logs || []).join('\n'); $('log').scrollTop = 1e9; lastLogSeq = seq; }
  } catch (e) {}
}
try {
  const cached = JSON.parse(localStorage.getItem('sw_snap_cache') || 'null');
  if (cached) renderSnap(cached, {}, '–');
} catch (e) {}
loadCfg().then(hasCk => {
  if (hasCk) api('/api/refresh', {async: true}).then(() => poll()).catch(() => {});
});
poll();
setInterval(poll, 2000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str, code: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str) -> None:
        if not path.is_file():
            self._json({"ok": False, "info": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            with STATE_LOCK:
                busy = bool(STATE["running"])
            self._json({"ok": True, "running": busy})
        elif path in ("/", "/index.html"):
            self._html(PAGE)
        elif path == "/assets/cookie_guide.png":
            self._file(COOKIE_GUIDE_PATH, "image/png")
        elif path == "/api/status":
            self._json(build_status())
        elif path == "/api/config":
            self._json({"ok": True, "cfg": safe_cfg_for_ui()})
        else:
            self._json({"ok": False, "info": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json({"ok": False, "info": "bad json"}, 400)
            return
        try:
            path = urlparse(self.path).path
            if path == "/api/config":
                self._json(do_update_cfg(payload))
            elif path == "/api/refresh":
                self._json(trigger_refresh_async() if payload.get("async") else do_refresh_snap())
            elif path == "/api/run":
                res = start_task(str(payload.get("mode") or "all"), source="manual")
                self._json(res, 200 if res.get("ok") else 409)
            elif path == "/api/stop":
                self._json(stop_task())
            else:
                self._json({"ok": False, "info": "not found"}, 404)
        except Exception:
            self._json({"ok": False, "info": traceback.format_exc()}, 500)

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    cfg = load_cfg()
    port = int(cfg["server"].get("port") or 50914)
    threading.Thread(target=sched_loop, daemon=True).start()
    threading.Thread(target=keepalive_loop, daemon=True).start()
    server = ThreadingHTTPServer((BIND_HOST, port), Handler)
    print(f"switched.cloud 定时签到控制台已启动（{BIND_HOST}:{port}，Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
