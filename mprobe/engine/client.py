# -*- coding: utf-8 -*-
"""模型调用客户端。

只用标准库 urllib，零依赖。支持两种协议：

    openai     POST {base_url}/chat/completions
               覆盖 OpenAI、DeepSeek、Qwen、Kimi、vLLM、one-api 中转等
    anthropic  POST {base_url}/messages

多轮题目在同一次请求里带完整对话历史；不同题目之间绝不复用上下文。
"""

import json
import random
import threading
import time
import urllib.error
import urllib.request


class ModelError(Exception):
    def __init__(self, msg, retryable=False):
        super().__init__(msg)
        self.retryable = retryable


class RateLimiter:
    """全局 QPS 限速，撞到 429 后会自动放慢。qps<=0 表示不限。

    为什么要「自动放慢」而不只是重试
    --------------------------------
    重试解决的是**这一条**请求，限速解决的是**后面 125 条**。对方一旦开始
    限流，说明当前速率就是超的——不改速率的话，重试成功之后下一条照样撞，
    整轮会变成「撞一次退避一次」，比一开始就慢下来还慢得多。

    放慢是**单向**的：本轮不再加速回去。一轮监控只有几分钟，探测对方限流
    边界的收益远小于再次触发限流的代价。
    """

    def __init__(self, qps):
        self.interval = (1.0 / qps) if qps and qps > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0
        self.slowdowns = 0

    def acquire(self):
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if wait:
            time.sleep(wait)

    def slow_down(self, factor=2.0, floor=1.0, cap=60.0):
        """撞到 429/限流之后放慢。返回新的间隔秒数。

        原本不限速（interval=0）时也会启用限速——「不限」只是没配，
        不代表对方没限。
        """
        with self._lock:
            self.interval = min(max(self.interval * factor, floor), cap)
            self.slowdowns += 1
            return self.interval


def retry_after(headers, default=None):
    """解析 Retry-After。支持「秒数」和「HTTP 日期」两种写法。

    对方明确告诉了要等多久，就不该再用自己那套指数退避去猜。
    """
    if not headers:
        return default
    v = (headers.get("Retry-After") or headers.get("retry-after") or "").strip()
    if not v:
        return default
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        import datetime
        dt = parsedate_to_datetime(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return max(0.0, (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
    except Exception:
        return default


def _opener(proxy):
    """proxy=None → 直连；proxy=URL → 走该代理。"""
    h = urllib.request.ProxyHandler({} if proxy is None
                                    else {"http": proxy, "https": proxy})
    return urllib.request.build_opener(h)


class ModelClient:
    """一次模型调用。

    关于代理
    --------
    Windows 上 urllib 会读注册表里的系统代理设置（curl 和 Node 都不读），
    于是「浏览器能开、Claude Code 能用，本工具却连不上」——因为只有本工具
    在走那个代理。公司内网地址尤其容易踩：本地代理客户端转不到内网，
    等 10 秒超时后返回一个**空响应体的 502**，看着像对方挂了。

    实测过一次：同一个地址，走系统代理 502（11.1 秒），强制直连 200（2.2 秒）。

    所以 `proxy` 默认是 `auto`：先按系统设置发，失败了自动改直连，
    一旦直连成功就锁定到本次运行结束，不再来回试。
    """

    def __init__(self, mcfg, run_cfg):
        self.cfg = mcfg
        self.style = mcfg["api_style"]
        self.base = mcfg["base_url"].rstrip("/")
        self.model = mcfg["model"]
        self.key = mcfg["_api_key"]
        self.timeout = mcfg["timeout"]
        self.retries = run_cfg["retries"]
        self.backoff = run_cfg["retry_backoff"]
        # 对方给的 Retry-After 可能长达几分钟，照单全收会让一轮跑不完。
        # 封顶之后仍然会重试，只是不至于一条请求把整轮拖死。
        self.max_retry_wait = run_cfg.get("max_retry_wait", 120)
        self.limiter = RateLimiter(run_cfg["qps"])

        mode = str(mcfg.get("proxy") or "auto").strip().lower()
        self.proxy_mode = mode
        self.proxy_note = ""
        self._lock = threading.Lock()
        if mode == "direct":
            self._chain = [("direct", _opener(None))]
        elif mode == "system":
            self._chain = [("system", urllib.request.build_opener())]
        elif mode.startswith("http"):
            self._chain = [("custom", _opener(mcfg["proxy"]))]
        else:                                   # auto：系统设置优先，失败退直连
            self._chain = [("system", urllib.request.build_opener()),
                           ("direct", _opener(None))]
            if not urllib.request.getproxies():
                self._chain = [("direct", _opener(None))]   # 本来就没代理，省一层

    def _send(self, url, payload, headers):
        """发一次请求，返回响应正文。auto 模式下代理失败会自动改直连并锁定。

        每次尝试都**新建** Request：`ProxyHandler` 会调 `req.set_proxy()` 改写
        请求对象本身，把 host 指向代理。复用同一个 Request 去重试，第二次照样
        发给代理——回退看着执行了，其实没换路。这个坑调了很久才发现。
        """
        chain = self._chain
        for i, (name, op) in enumerate(chain):
            fallback_left = i < len(chain) - 1
            req = urllib.request.Request(url, data=payload, headers=dict(headers),
                                         method="POST")
            try:
                with op.open(req, timeout=self.timeout) as r:
                    body = r.read().decode("utf-8", "replace")
                if name == "direct" and len(chain) > 1:
                    self._latch_direct(op)
                return body
            except urllib.error.HTTPError as e:
                # HTTPError 的正文只能读一次，读出来挂在异常上给上层用
                e.body = e.read().decode("utf-8", "replace")
                # 空响应体的 5xx 很可能是**代理**给的，不是目标服务给的
                if fallback_left and e.code >= 500 and not e.body.strip():
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError):
                if fallback_left:
                    continue
                raise

    def _latch_direct(self, op):
        """直连成功就锁定，本次运行不再试代理——否则每条请求都要先超时一次。"""
        with self._lock:
            if len(self._chain) > 1:
                self._chain = [("direct", op)]
                self.proxy_note = ("系统代理访问不了这个地址，已自动改为直连。"
                                   "想固定下来，把配置里的 proxy 设成 direct。")

    # -- 协议适配 ---------------------------------------------------------

    def _build(self, messages):
        # temperature 留空（null）就**不发这个字段**。新一代思考型模型已经
        # 弃用它，硬发会被 400 顶回来：「`temperature` is deprecated for this
        # model」。监控本来靠它锁复现性，发不了就只能靠多次采样压方差——
        # 好在 σ 是实测出来的，方差变大会自动体现在阈值上。
        temp = self.cfg.get("temperature")
        if self.style == "openai":
            url = self.base + "/chat/completions"
            headers = {"Content-Type": "application/json",
                       "Authorization": "Bearer " + self.key}
            body = {"model": self.model, "messages": messages,
                    "max_tokens": self.cfg["max_tokens"]}
            if temp is not None:
                body["temperature"] = temp
        else:
            url = self.base + "/messages"
            headers = {"Content-Type": "application/json",
                       "x-api-key": self.key,
                       "anthropic-version": "2023-06-01"}
            sys_msgs = [m["content"] for m in messages if m["role"] == "system"]
            body = {"model": self.model,
                    "messages": [m for m in messages if m["role"] != "system"],
                    "max_tokens": self.cfg["max_tokens"]}
            if temp is not None:
                body["temperature"] = temp
            if sys_msgs:
                body["system"] = "\n\n".join(sys_msgs)
        headers.update(self.cfg.get("extra_headers") or {})
        body.update(self.cfg.get("extra_body") or {})
        return url, headers, body

    def _parse(self, data):
        try:
            if self.style == "openai":
                ch = data["choices"][0]
                text = (ch.get("message") or {}).get("content") or ""
                usage = data.get("usage") or {}
                pd = usage.get("prompt_tokens_details") or {}
                cd = usage.get("completion_tokens_details") or {}
                return text, {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "cached_tokens": pd.get("cached_tokens"),
                    "reasoning_tokens": cd.get("reasoning_tokens"),
                    "finish_reason": ch.get("finish_reason"),
                }
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            usage = data.get("usage") or {}
            # Anthropic 把缓存读取单列，且不计入 input_tokens
            cached = usage.get("cache_read_input_tokens") or 0
            return text, {
                "prompt_tokens": (usage.get("input_tokens") or 0) + cached
                                 + (usage.get("cache_creation_input_tokens") or 0),
                "completion_tokens": usage.get("output_tokens"),
                "cached_tokens": cached or None,
                "reasoning_tokens": None,
                "finish_reason": data.get("stop_reason"),
            }
        except (KeyError, IndexError, TypeError) as e:
            raise ModelError("响应结构无法解析（%s）: %s" % (e, json.dumps(data)[:400]))

    # -- 调用 -------------------------------------------------------------

    def chat(self, messages):
        """返回 (text, meta)。meta 含 latency_ms / tokens / attempts。"""
        url, headers, body = self._build(messages)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last = None
        hint = None          # 对方给的 Retry-After，优先于自己的退避
        for attempt in range(1, self.retries + 2):
            self.limiter.acquire()
            t0 = time.monotonic()
            try:
                raw = self._send(url, payload, headers)
                dt = (time.monotonic() - t0) * 1000
                text, meta = self._parse(json.loads(raw))
                meta.update({"latency_ms": round(dt, 1), "attempts": attempt, "ok": True})
                return text, meta

            except urllib.error.HTTPError as e:
                detail = getattr(e, "body", "")[:300]
                retryable = e.code in (408, 409, 425, 429, 500, 502, 503, 504)
                # 空响应体的 5xx 最难查：界面上只剩一个「HTTP 502」，看不出
                # 是自己填错了还是对面挂了。直接把判断写进报错里。
                if not detail.strip() and e.code >= 500:
                    detail = ("返回了空响应体的 %d。这不是配置错——地址或密钥填错"
                              "会得到带说明的 4xx。常见原因：① 请求被系统代理"
                              "拦下了（内网地址尤其容易，本工具已自动试过直连）；"
                              "② 对方服务或其上游有问题。" % e.code)
                    if self.proxy_mode not in ("direct",) and urllib.request.getproxies():
                        detail += "　当前系统代理：%s" % (
                            urllib.request.getproxies().get("http") or "?")
                if e.code in (429, 503):
                    # 被限流了：这一条按对方给的时间等，**后面所有条**永久放慢。
                    hint = retry_after(e.headers)
                    iv = self.limiter.slow_down()
                    detail += "（已自动降速到每 %.1f 秒一条）" % iv
                last = ModelError("HTTP %s: %s" % (e.code, detail), retryable)
            except urllib.error.URLError as e:
                last = ModelError("网络错误: %s" % e.reason, True)
            except TimeoutError:
                last = ModelError("请求超时（%ss）" % self.timeout, True)
            except json.JSONDecodeError as e:
                last = ModelError("返回不是合法 JSON: %s" % e, True)
            except ModelError as e:
                last = e

            if not getattr(last, "retryable", False) or attempt > self.retries:
                break
            # 对方给了 Retry-After 就听它的——自己猜的退避经常比它短一个数量级。
            # 抖动是必须的：同一批并发请求若同时醒来，会一起再撞一次限流。
            wait = self.backoff ** (attempt - 1) * (1 + random.random() * 0.3)
            if hint is not None:
                wait = max(wait, min(hint, self.max_retry_wait)) * (1 + random.random() * 0.2)
                hint = None
            time.sleep(wait)

        return None, {"ok": False, "error": str(last), "attempts": attempt,
                      "latency_ms": None, "slowdowns": self.limiter.slowdowns}

    def probe(self):
        """连通性自检：发一条最短请求。"""
        text, meta = self.chat([{"role": "user", "content": "ping"}])
        if not meta.get("ok"):
            raise ModelError("连通性自检失败：%s" % meta.get("error"))
        return (text or "")[:60], meta
