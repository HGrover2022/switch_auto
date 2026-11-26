"""switched.cloud 纯 HTTP 协议核心（连签、快照、会话/VPS 保活、adfocus、linkvertise）。

说明：
- 仅实现纯 HTTP / GraphQL 可稳定完成的任务；
- cuty（Cloudflare Turnstile）、vylo（PoW + 人机验证）、cpx_research（问卷）不在服务端绕过，
  仅在 snapshot 中展示其今日进度，需人工在浏览器处理。
"""
from __future__ import annotations

import base64
import json
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA

PANEL_ORIGIN_DEFAULT = "https://panel.switched.cloud"
LV_GRAPHQL = "https://publisher.linkvertise.com/graphql"
LV_JS_URL = "https://publisher.linkvertise.com/cdn/linkvertise.js"
TABOOLA_SYNC = (
    "https://api.taboola.com/2.0/json/linkvertisenew-singlecardexperience/"
    "user.sync?app.type=desktop&app.apikey=ef1d95992fae5ed0b4440e58cae147e9a1f7ffea"
)

SUPPORTED_PROVIDERS = {"adfocus", "linkvertise"}
MANUAL_ONLY_PROVIDERS = {
    "cuty": "含 Turnstile 验证，需浏览器手动",
    "vylo": "含 PoW/人机验证，需浏览器手动",
    "cpx_research": "问卷调查，需手动完成",
}

LV_Q_GET = """query getContent($identifier: PublicLinkIdentificationInput!, $task_args: TaskArgument) {
  getContent(input: $identifier, task_args: $task_args) {
    ... on ContentAccessTaskSet {
      __typename
      tasks {
        __typename
        id
        ... on PremiumTask {
          __typename
          status
          id
        }
        ... on WaitTask {
          __typename
          id
          remainingWaitingTime
          status
          adsTotal
        }
        ... on AdTask {
          __typename
          id
          status
          payloadBag {
            taboola {
              session_id
              __typename
            }
            __typename
          }
          ads {
            ...AdsFragment
            __typename
          }
          adIndex
          adsTotal
        }
      }
    }
    ... on DetailPageTargetData {
      type
      url
      paste
      __typename
    }
    __typename
  }
}

fragment AdsFragment on CustomAdOffer {
  completion_token
  call_to_action
  countdown
  description
  provider
  title
  provider_additional_payload {
    taboola {
      available_event_url
      visible_event_url
      __typename
    }
    __typename
  }
  clickout_action {
    type
    ... on CustomAdOfferClickoutUrlAction {
      type
      clickout_url
      __typename
    }
    __typename
  }
  media {
    type
    ... on UrlMediaResource {
      content_type
      resource_url
      __typename
    }
    __typename
  }
  __typename
}"""

LV_Q_DONE = """mutation completeTask($identifier: PublicLinkIdentificationInput!, $task_id: String!, $task_args: TaskArgument) {
  completeTask(input: $identifier, task_id: $task_id, task_args: $task_args) {
    id
    ... on AdTask {
      __typename
      id
      status
    }
    ... on PremiumTask {
      __typename
      status
      id
    }
    ... on WaitTask {
      __typename
      id
      status
      remainingWaitingTime
      adsTotal
    }
    __typename
  }
}"""


class SessionExpiredError(RuntimeError):
    """面板 Cookie 失效或跳转到 /login。"""


class ProviderError(RuntimeError):
    """渠道执行失败；cool_s 非 0 时表示服务端给出了明确的风控冷却秒数。"""

    def __init__(self, message: str, cool_s: int = 0) -> None:
        super().__init__(message)
        self.cool_s = int(cool_s or 0)


def _sleep_cancelable(seconds: float, stop_event: threading.Event | None) -> bool:
    """可中断等待：返回 True 表示收到停止信号。"""
    if seconds <= 0:
        return bool(stop_event and stop_event.is_set())
    if stop_event is None:
        time.sleep(seconds)
        return False
    return stop_event.wait(seconds)


def parse_cookie_string(cookie_str: str) -> dict[str, str]:
    raw = re.sub(r"^\s*cookie\s*:?\s*", "", str(cookie_str or ""), flags=re.I)
    raw = re.sub(r"[\r\n\t]+", " ", raw).strip()
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def format_cookie_dict(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k)


def _op_id() -> str:
    """复刻原脚本 100 字符 action_id（3 个 uuid4 按 [8,4,4,4,20,...] 拼接）。"""
    a, b, c = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    return (
        a[:23] + "-" + a[24:] + b[:8]
        + "-" + b[9:23] + "-" + b[24:] + c[:8]
        + "-" + c[9:13] + "-" + c[14:18] + "-" + c[19:23] + "-" + c[24:28]
    )


def _adfoc_state_a(key: str) -> dict:
    base = 1560 + random.randint(0, 180)
    return {
        "k": key, "r": 100, "y": "a", "e": 0,
        "n": {
            "b": random.randint(400, 520),
            "p": base + random.randint(20, 90),
            "d": base + random.randint(0, 60),
            "l": base + random.randint(20, 100),
        },
        "rf": base + random.randint(20, 80),
        "t0": base + random.randint(0, 60),
        "c": random.randint(2800, 3800),
        "u": {"k": 2, "s": 1, "y": 0, "w": random.randint(1500, 1920), "h": random.randint(300, 420), "c": 1},
        "fl": -1, "ft": -1, "lb": 1, "v": 1, "hm": 0, "hn": 0, "mt": 0, "ti": 6, "cn": 5,
        "jq": 0, "js": -1, "ss": -1, "ac": 0, "pv": 0,
        "sc": [random.randint(1600, 2560), random.randint(900, 1440), random.randint(1400, 1900), random.randint(300, 600), 100],
        "lg": "zh-CN",
        "tz": "Asia/Shanghai",
        "ch": None, "bt": 1, "g": 2, "a": 1, "ct": 0,
    }


def _adfoc_state_b(key: str) -> dict:
    ss = random.randint(8200, 11800)
    return {
        "k": key, "r": 100, "y": "b", "x": 1, "ss": ss,
        "sk": ss + random.randint(900, 2200),
        "lv": ss + random.randint(900, 2200),
        "hm": 0, "ac": 0, "cn": 0,
    }


@dataclass
class SwitchedClient:
    origin: str = PANEL_ORIGIN_DEFAULT
    cookie_str: str = ""
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    timeout: int = 30
    on_cookie_update: Callable[[str], None] | None = None
    _lv_key: RSA.RsaKey | None = field(default=None, init=False, repr=False)
    _csrf: str = field(default="", init=False, repr=False)
    _lv_uid: str = field(default="", init=False, repr=False)
    _session: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.origin = (self.origin or PANEL_ORIGIN_DEFAULT).rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self.load_cookie_str(self.cookie_str)

    def load_cookie_str(self, cookie_str: str) -> None:
        self.cookie_str = (cookie_str or "").strip()
        host = urlparse(self.origin).hostname or "panel.switched.cloud"
        self._session.cookies.clear()
        for k, v in parse_cookie_string(self.cookie_str).items():
            self._session.cookies.set(k, v, domain=host, path="/")

    def export_panel_cookie_str(self) -> str:
        host = urlparse(self.origin).hostname or "panel.switched.cloud"
        merged = parse_cookie_string(self.cookie_str)
        for c in self._session.cookies:
            if not c.domain or c.domain.lstrip(".") == host or host.endswith("." + c.domain.lstrip(".")):
                merged[c.name] = c.value
        return format_cookie_dict(merged)

    def _sync_cookies(self) -> None:
        updated = self.export_panel_cookie_str()
        if updated and updated != self.cookie_str:
            self.cookie_str = updated
            if self.on_cookie_update:
                self.on_cookie_update(updated)

    def _abs(self, href: str) -> str:
        return urljoin(self.origin + "/", href or "")

    def _panel_get(self, path_or_url: str, *, allow_redirects: bool = True) -> requests.Response:
        if not self.cookie_str:
            raise SessionExpiredError("未配置 panel.switched.cloud Cookie")
        url = path_or_url if path_or_url.startswith("http") else self._abs(path_or_url)
        last_exc: Exception | None = None
        r: requests.Response | None = None
        for attempt in range(2):
            try:
                r = self._session.get(url, timeout=self.timeout, allow_redirects=allow_redirects)
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(1.5)
        if r is None:
            raise RuntimeError(f"面板网络请求异常: {last_exc}") from last_exc
        self._sync_cookies()
        if r.status_code == 401 or (r.history and "/login" in r.url) or "/login" in urlparse(r.url).path:
            raise SessionExpiredError("面板会话已失效（跳转到 /login），请重新粘贴 Cookie")
        if r.status_code >= 400:
            raise RuntimeError(f"面板请求失败 HTTP {r.status_code}: {url}")
        return r

    def ping_session(self) -> bool:
        self._panel_get("/tokens")
        return True

    def list_vps(self) -> list[str]:
        r = self._panel_get("/vps")
        ids = re.findall(r"/vps/([0-9a-f-]{36})", r.text or "", re.I)
        return list(dict.fromkeys(ids))

    def ping_vps(self, uuids: list[str] | None = None) -> list[str]:
        ids = uuids if uuids is not None else self.list_vps()
        ok_ids = []
        for vid in ids:
            self._panel_get(f"/vps/{vid}")
            ok_ids.append(vid)
        return ok_ids

    def snapshot(self) -> dict:
        r = self._panel_get("/tokens")
        html = r.text or ""
        soup = BeautifulSoup(html, "html.parser")

        tok_el = soup.select_one('input[name="_token"]')
        if tok_el and tok_el.get("value"):
            self._csrf = str(tok_el["value"])

        m_uid = re.search(r"var\s+userId\s*=\s*(\d+)", html)
        if m_uid:
            self._lv_uid = m_uid.group(1)

        providers = []
        for row in soup.select(".pct-way"):
            name_el = row.select_one(".pct-way-name")
            pays_el = row.select_one(".pct-way-pays")
            notes = [(n.get_text() or "").strip() for n in row.select(".pct-note")]
            inp = row.select_one('input[name="provider"]')
            anchor = row.select_one('a[href*="/tokens/return/"]')
            name_txt = (name_el.get_text() or "").strip() if name_el else ""
            by_name = ""
            if re.match(r"^linkvertise$", name_txt, re.I):
                by_name = "linkvertise"
            elif re.match(r"^cuty", name_txt, re.I):
                by_name = "cuty"
            elif re.match(r"^adfoc", name_txt, re.I):
                by_name = "adfocus"
            elif re.match(r"^vylo", name_txt, re.I):
                by_name = "vylo"
            elif re.match(r"^cpx", name_txt, re.I):
                by_name = "cpx_research"
            key = (inp.get("value") if inp else "") or ("linkvertise" if anchor else "") or by_name
            if not key:
                continue

            granted, cap = 0, 0
            for note in notes:
                cm = re.search(r"(\d+)\s+of\s+(\d+)\s+today", note, re.I)
                if cm:
                    granted, cap = int(cm.group(1)), int(cm.group(2))
                    break
            pm = re.search(r"([\d.]+)", (pays_el.get_text() or "") if pays_el else "")
            providers.append({
                "provider": key,
                "name": name_txt or key,
                "granted_today": granted,
                "daily_cap": cap,
                "left": max(0, cap - granted),
                "quote": float(pm.group(1)) if pm else 0.0,
                "anchor": self._abs(anchor["href"]) if (anchor and anchor.get("href")) else None,
                "full": cap > 0 and granted >= cap,
                "supported": key in SUPPORTED_PROVIDERS,
                "manual_reason": MANUAL_ONLY_PROVIDERS.get(key, ""),
            })

        bal_main, bal_sub = None, None
        bal_el = soup.select_one(".pct .pct-balance")
        if bal_el:
            bal_main = (bal_el.get_text() or "").strip()
        pct = soup.select_one(".pct")
        if pct:
            for n in pct.select(".pct-note"):
                fm = re.match(r"^([\d.]+)\s+of a Cloud Token", (n.get_text() or "").strip())
                if fm:
                    bal_sub = fm.group(1)
                    break

        streak = {"text": "–", "claimable": False, "action": None, "token": None}
        for sec in soup.select("section"):
            txt = sec.get_text(" ", strip=True)
            if not re.search(r"Daily streak", txt, re.I):
                continue
            dm = re.search(r"Claimed today \(day (\d+)\)", txt, re.I)
            if dm:
                streak = {"text": f"D{dm.group(1)}", "claimable": False, "action": None, "token": None}
            else:
                form = sec.select_one("form")
                if form:
                    tk = form.select_one('input[name="_token"]')
                    streak = {
                        "text": "可领",
                        "claimable": True,
                        "action": self._abs(form.get("action") or ""),
                        "token": str(tk["value"]) if (tk and tk.get("value")) else self._csrf,
                    }
            break

        if not providers and bal_main is None:
            raise SessionExpiredError("面板快照为空（Cookie 可能已失效或页面结构变更）")

        return {
            "providers": providers,
            "balance": {"main": bal_main, "sub": bal_sub},
            "streak": streak,
            "lv_uid": self._lv_uid,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def claim_streak(self, snap: dict | None = None) -> dict:
        s = (snap or self.snapshot()).get("streak") or {}
        if not s.get("claimable") or not s.get("action"):
            return {"ok": True, "claimed": False, "info": f"连签当前状态：{s.get('text', '–')}"}
        r = self._session.post(
            s["action"],
            data={"_token": s.get("token") or self._csrf},
            headers={"Referer": f"{self.origin}/tokens"},
            timeout=self.timeout,
            allow_redirects=True,
        )
        self._sync_cookies()
        if r.status_code >= 400:
            raise RuntimeError(f"连签提交失败 HTTP {r.status_code}")
        return {"ok": True, "claimed": True, "info": "连签奖励已领取"}

    def _hit_back(self, url: str) -> bool:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                r = self._session.get(
                    url,
                    headers={
                        "Referer": self.origin + "/",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                self._sync_cookies()
                return r.status_code < 400
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(1.5)
        raise ProviderError(f"回跳入账网络异常：{last_exc}") from last_exc

    # ---------------- adfoc.us 纯 HTTP 直刷 ----------------
    def run_adfocus(self, log: Callable[[str], None] = print,
                    stop_event: threading.Event | None = None) -> bool:
        self.snapshot()
        t0 = time.monotonic()
        r = self._session.post(
            f"{self.origin}/tokens/earn",
            data={"_token": self._csrf, "provider": "adfocus"},
            headers={
                "Referer": f"{self.origin}/tokens",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=self.timeout,
            allow_redirects=False,
        )
        self._sync_cookies()
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            entry_url = urljoin(self.origin + "/", r.headers["Location"])
            html = ""
        elif r.status_code >= 400:
            raise ProviderError(f"earn 返回 HTTP {r.status_code}")
        else:
            entry_url = r.url
            html = r.text or ""

        ad_sess = requests.Session()
        ad_sess.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        try:
            if len(html) < 500 or "my_key" not in html:
                page = ad_sess.get(
                    entry_url,
                    headers={
                        "Referer": f"{self.origin}/tokens",
                        **AD_DOC_HEADERS,
                    },
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                if page.status_code != 200:
                    raise ProviderError(f"adfocus 页面 HTTP {page.status_code}")
                entry_url = page.url
                html = page.text or ""

            m_key = (
                re.search(r'id=["\']my_key["\'][^>]*value=["\']([^"\']+)["\']', html)
                or re.search(r'value=["\']([^"\']+)["\'][^>]*id=["\']my_key["\']', html)
            )
            if not m_key:
                raise ProviderError(f"adfocus 页面未找到 my_key（{len(html)}B）")
            key = m_key.group(1)

            m_back = (
                re.search(r'id=["\']showSkip["\'][\s\S]{0,600}?href=["\']([^"\']+)["\']', html)
                or re.search(r'href=["\']([^"\']*/(?:tokens/back|claim=)[^"\']*)["\']', html)
            )
            back_url = m_back.group(1) if m_back else ""
            nx = "/serve/nx/" in html
            base = "https://adfoc.us/serve/nx" if nx else "https://adfoc.us/serve"
            m_next = re.search(r"/serve/(?:nx/)?next\?t=([A-Za-z0-9_\-]+)", html)
            next_tok = m_next.group(1) if m_next else ""

            common = {
                "Origin": "https://adfoc.us",
                "Referer": entry_url,
                "X-Requested-With": "XMLHttpRequest",
            }
            try:
                ad_sess.post(base + "/credit", data={"key": key}, headers=common, timeout=20)
            except Exception as exc:
                log(f"[adfocus] credit 请求异常：{exc}")
            if _sleep_cancelable(random.uniform(1.1, 2.1), stop_event):
                return False
            try:
                ad_sess.post(
                    base + "/state",
                    data=json.dumps(_adfoc_state_a(key)),
                    headers={**common, "Content-Type": "text/plain;charset=UTF-8"},
                    timeout=20,
                )
            except Exception as exc:
                log(f"[adfocus] state(a) 请求异常：{exc}")
            wait_b = max(1.2, 8.5 - (time.monotonic() - t0))
            if _sleep_cancelable(wait_b, stop_event):
                return False
            try:
                ad_sess.post(
                    base + "/state",
                    data=json.dumps(_adfoc_state_b(key)),
                    headers={**common, "Content-Type": "text/plain;charset=UTF-8"},
                    timeout=20,
                )
            except Exception as exc:
                log(f"[adfocus] state(b) 请求异常：{exc}")
            if _sleep_cancelable(random.uniform(0.5, 1.1), stop_event):
                return False
            try:
                ad_sess.post(base + "/skip", data={"key": key}, headers=common, timeout=20)
            except Exception as exc:
                log(f"[adfocus] skip 请求异常：{exc}")

            if back_url:
                left = 11.0 - (time.monotonic() - t0)
                if left > 0 and _sleep_cancelable(left, stop_event):
                    return False
                full_back = urljoin(entry_url, back_url)
                if not self._hit_back(full_back):
                    raise ProviderError("adfocus 回跳地址未入账")
                return True

            if next_tok:
                if _sleep_cancelable(random.uniform(0.4, 0.9), stop_event):
                    return False
                nx_url = f"https://adfoc.us/serve/{'nx/' if nx else ''}next?t={quote(next_tok)}"
                nx2 = ad_sess.get(nx_url, headers={"Referer": entry_url}, timeout=self.timeout, allow_redirects=True)
                if "claim=" in nx2.url or "/tokens/back" in nx2.url:
                    if not self._hit_back(nx2.url):
                        raise ProviderError("adfocus next 回跳未入账")
                    return True
                raise ProviderError(f"adfocus 未拿到回跳地址（status {nx2.status_code}）")

            raise ProviderError("adfocus 页面既无回跳地址也无 next 令牌")
        finally:
            ad_sess.close()

    # ---------------- linkvertise GraphQL 纯 HTTP 直刷 ----------------
    def _lv_public_key(self) -> RSA.RsaKey:
        if self._lv_key is not None:
            return self._lv_key
        r = requests.get(LV_JS_URL, headers={"User-Agent": self.user_agent}, timeout=20)
        r.raise_for_status()
        m = re.search(r"-----BEGIN PUBLIC KEY-----([\s\S]*?)-----END PUBLIC KEY-----", r.text or "")
        if not m:
            raise ProviderError("linkvertise.js 未解析到公钥")
        body = re.sub(r"\\[nrt]", "", m.group(1))
        body = re.sub(r"\s+", "", body)
        pem = "-----BEGIN PUBLIC KEY-----\n" + body + "\n-----END PUBLIC KEY-----"
        self._lv_key = RSA.import_key(pem)
        return self._lv_key

    def lv_encode(self, target_url: str) -> str:
        """复刻 linkvertise.js：前 ~66 字节 RSA-OAEP(SHA-256)，其余原样拼接。"""
        key = self._lv_public_key()
        chars = list(unquote(target_url))
        head = bytearray()
        idx = 0
        while idx < len(chars):
            head.extend(chars[idx].encode("utf-8"))
            idx += 1
            if len(head) > 66:
                break
        tail = "".join(chars[idx:])
        cipher = PKCS1_OAEP.new(key, hashAlgo=SHA256)
        ct = cipher.encrypt(bytes(head))
        return base64.b64encode(ct).decode("ascii") + tail

    def _lv_post(
        self,
        name: str,
        body: dict,
        auth: str = "",
        ut: str = "",
        sess: requests.Session | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        url = f"{LV_GRAPHQL}?name={quote(name)}" + (f"&X-Linkvertise-UT={quote(ut)}" if ut else "")
        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://linkvertise.com",
            "Referer": "https://linkvertise.com/",
        }
        if extra_headers:
            headers.update(extra_headers)
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        poster = sess or requests
        r: requests.Response | None = None
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                r = poster.post(url, headers=headers, json=body, timeout=60)
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(2.0)
        if r is None:
            raise ProviderError(f"linkvertise 网络异常：{last_exc}") from last_exc
        try:
            j = r.json()
        except Exception as exc:
            raise ProviderError(f"linkvertise 返回非 JSON：{r.text[:140]}") from exc
        if j.get("errors"):
            raise ProviderError("linkvertise: " + json.dumps(j["errors"][0], ensure_ascii=False)[:200])
        return j.get("data") or {}

    def _fire_async(self, url: str, timeout: int = 10, on_json: Callable[[dict], None] | None = None) -> None:
        def _bg() -> None:
            try:
                resp = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=timeout)
                if on_json:
                    data = resp.json()
                    if isinstance(data, dict):
                        on_json(data)
            except Exception:
                pass
        threading.Thread(target=_bg, daemon=True).start()

    @staticmethod
    def _lv_page_url(uid: str, rand_s: str, enc_hash: str) -> str:
        return f"https://linkvertise.com/access/{uid}/{rand_s}/dynamic?r={quote(enc_hash, safe='')}&v=2"

    def _lv_args(self, uid: str, rand_s: str, enc_hash: str, sid: str | None, tab_uid: str | None) -> dict:
        return {
            "additional_data": {
                "taboola": {
                    "user_id": tab_uid or "fallbackUserId",
                    "consent_string": "",
                    "url": self._lv_page_url(uid, rand_s, enc_hash),
                    "external_referrer": self.origin + "/",
                    "session_id": sid,
                }
            },
            "action_id": _op_id(),
        }

    def run_linkvertise(self, auth: str = "", ut: str = "",
                        log: Callable[[str], None] = print,
                        stop_event: threading.Event | None = None) -> bool:
        snap = self.snapshot()
        lv_row = next((p for p in snap["providers"] if p["provider"] == "linkvertise"), None)
        if not lv_row or not lv_row.get("anchor"):
            raise ProviderError("面板没有可用的 linkvertise 入口")
        lv_uid = snap.get("lv_uid") or self._lv_uid
        if not lv_uid:
            raise ProviderError("页面未解析到 linkvertise userId")

        enc_hash = self.lv_encode(lv_row["anchor"])
        rand_s = str(random.random() * 1000.0)
        page_url = self._lv_page_url(str(lv_uid), rand_s, enc_hash)
        complete_hdrs = {"cqreferrer": page_url}
        ident = {
            "userIdAndHash": {
                "user_id": str(lv_uid),
                "hash": enc_hash,
                "originates_from_adfly": False,
                "version": "2",
            }
        }

        tab_ref: dict[str, str | None] = {"id": None}
        def _on_taboola_sync(tj: dict) -> None:
            if isinstance(tj.get("user"), dict) and tj["user"].get("id"):
                tab_ref["id"] = str(tj["user"]["id"])
        self._fire_async(TABOOLA_SYNC, timeout=12, on_json=_on_taboola_sync)

        lv_sess = requests.Session()
        lv_sess.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        sid: str | None = None
        last_ad_key = ""
        t0 = time.monotonic()
        wait_guard_id, wait_guard_n = None, 0

        try:
            for round_i in range(26):
                if stop_event and stop_event.is_set():
                    return False
                if time.monotonic() - t0 > 240:
                    raise ProviderError("linkvertise 任务链超过 4 分钟无进展，放弃本轮")
                data = self._lv_post(
                    "getContent",
                    {
                        "operationName": "getContent",
                        "variables": {
                            "identifier": ident,
                            "task_args": self._lv_args(str(lv_uid), rand_s, enc_hash, sid, tab_ref["id"]),
                        },
                        "query": LV_Q_GET,
                    },
                    auth=auth,
                    ut=ut,
                    sess=lv_sess,
                )
                c = data.get("getContent") or {}
                if not c:
                    raise ProviderError("linkvertise getContent 空返回")
                if c.get("__typename") == "DetailPageTargetData" or c.get("url"):
                    target = c.get("url")
                    if not target or not self._hit_back(target):
                        raise ProviderError("linkvertise 回跳地址未入账")
                    return True

                tasks = c.get("tasks") or []
                w = next((t for t in tasks if t.get("__typename") == "WaitTask" and t.get("status") != "DONE"), None)
                if w:
                    left = w.get("remainingWaitingTime")
                    if left is not None and float(left) > 90:
                        cool = max(int(float(left)) + 60, 300)
                        raise ProviderError(f"linkvertise 风控闸门要求等待 {int(float(left))}s，本轮跳过", cool_s=cool)
                    if wait_guard_id == w.get("id"):
                        wait_guard_n += 1
                        if wait_guard_n >= 2:
                            raise ProviderError("linkvertise 等待任务完成两次仍未推进（疑似风控）", cool_s=1800)
                    else:
                        wait_guard_id, wait_guard_n = w.get("id"), 1
                    if left is not None and float(left) > 0:
                        wait_s = float(left) + 0.8
                        log(f"[linkvertise] 等待任务 {wait_s:.1f}s…")
                    else:
                        wait_s = random.uniform(4.0, 4.7)
                    if _sleep_cancelable(wait_s, stop_event):
                        return False
                    self._lv_post(
                        "completeTask",
                        {
                            "operationName": "completeTask",
                            "variables": {
                                "identifier": ident,
                                "task_id": w["id"],
                                "task_args": self._lv_args(str(lv_uid), rand_s, enc_hash, sid, tab_ref["id"]),
                            },
                            "query": LV_Q_DONE,
                        },
                        auth=auth,
                        ut=ut,
                        sess=lv_sess,
                        extra_headers=complete_hdrs,
                    )
                    if _sleep_cancelable(random.uniform(1.4, 1.8), stop_event):
                        return False
                    continue

                ad_task = next((t for t in tasks if t.get("__typename") == "AdTask" and t.get("status") != "DONE"), None)
                if ad_task:
                    bag = ((ad_task.get("payloadBag") or {}).get("taboola") or {})
                    if bag.get("session_id"):
                        sid = bag["session_id"]
                    ads = ad_task.get("ads") or []
                    if not ads:
                        raise ProviderError("linkvertise AdTask 没有 ad 可看")
                    ad = ads[0]
                    ad_prov = str(ad.get("provider") or "UNKNOWN")
                    ad_key = f"{ad_task.get('id')}|{ad.get('completion_token')}"
                    if ad_key != last_ad_key:
                        last_ad_key = ad_key
                        ev = ((ad.get("provider_additional_payload") or {}).get("taboola") or {})
                        if _sleep_cancelable(random.uniform(1.4, 2.0), stop_event):
                            return False
                        if ev.get("available_event_url"):
                            self._fire_async(ev["available_event_url"], timeout=10)
                        if _sleep_cancelable(random.uniform(1.1, 1.8), stop_event):
                            return False
                        if ev.get("visible_event_url"):
                            self._fire_async(ev["visible_event_url"], timeout=10)
                        wait_s = random.uniform(15.6, 17.6)
                        log(f"[linkvertise] 广告位 {ad_task.get('adIndex')}/{ad_task.get('adsTotal')}（{ad_prov}），停留 {wait_s:.1f}s")
                        if _sleep_cancelable(wait_s, stop_event):
                            return False

                    t_args = self._lv_args(str(lv_uid), rand_s, enc_hash, sid, tab_ref["id"])
                    if ad.get("completion_token"):
                        t_args["completion_token"] = ad.get("completion_token")
                    try:
                        done = self._lv_post(
                            "completeTask",
                            {
                                "operationName": "completeTask",
                                "variables": {"identifier": ident, "task_id": ad_task["id"], "task_args": t_args},
                                "query": LV_Q_DONE,
                            },
                            auth=auth,
                            ut=ut,
                            sess=lv_sess,
                            extra_headers=complete_hdrs,
                        )
                    except ProviderError:
                        if not t_args.get("completion_token"):
                            raise
                        fallback_args = self._lv_args(str(lv_uid), rand_s, enc_hash, sid, tab_ref["id"])
                        done = self._lv_post(
                            "completeTask",
                            {
                                "operationName": "completeTask",
                                "variables": {"identifier": ident, "task_id": ad_task["id"], "task_args": fallback_args},
                                "query": LV_Q_DONE,
                            },
                            auth=auth,
                            ut=ut,
                            sess=lv_sess,
                            extra_headers=complete_hdrs,
                        )
                    back = done.get("completeTask") or {}
                    if back.get("status") and back["status"] != "DONE":
                        raise ProviderError(f"linkvertise completeTask 未完成：{back['status']}")
                    early = back.get("url")
                    if early:
                        if not self._hit_back(early):
                            raise ProviderError("linkvertise 回跳地址未入账")
                        return True
                    if _sleep_cancelable(random.uniform(1.4, 1.8), stop_event):
                        return False
                    continue

                names = ",".join(f"{t.get('__typename')}:{t.get('status')}" for t in tasks)
                if any(t.get("__typename") == "PremiumTask" for t in tasks):
                    if round_i < 3:
                        if _sleep_cancelable(2.5, stop_event):
                            return False
                        continue
                    raise ProviderError(f"linkvertise 只剩付费任务（{names}）", cool_s=3600)
                raise ProviderError(f"linkvertise 没有可执行任务（{names or '空'}）")

            raise ProviderError("linkvertise 任务链轮次超限")
        finally:
            lv_sess.close()
