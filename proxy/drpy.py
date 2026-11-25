#!/usr/bin/env python3
"""drpy2 单文件源（TVBox 规则 ``.js``）适配层。

一个 drpy 源就是一段 ``var rule = {...}`` 的 JS，里面用选择器描述「分类 /
列表 / 详情 / 搜索 / 播放」。本项目不跑 Node、也不内嵌 JS 引擎：只把 ``rule``
这个**对象字面量**解析成 Python dict，然后用 selectolax 执行里面的选择器。

支持的键（两个随源文件已实测通过：dbku.js / 6v影视.js）：

``host`` / ``headers`` / ``timeout``
    站点根地址与请求头。
``class_parse``
    ``选择器;名称;链接;正则``，正则第 1 组或链接本身就是分类 id。
``url`` / ``一级`` / ``推荐``
    分类分页地址模板（``fyclass`` / ``fypage`` / ``fyfilter``，``[首页地址]``
    表示第 1 页与后续页不同）与列表规则 ``列表选择器;标题;图片;备注;链接``。
``二级``
    ``{title, img, desc, content, tabs, lists}``；每项可用 ``;`` 写多条兜底。
``searchUrl`` / ``搜索``
    搜索地址，``#`` 后面是 POST 表单体，``**`` 是关键词，结尾 ``;post`` 表示
    用 POST；``搜索`` 规则与 ``一级`` 同形。
``lazy`` / ``play_parse``
    **不执行**。``lazy`` 是任意 JS，没有引擎跑不了；播放地址改走内置的通用
    播放页解析（MacCMS 的 ``player_data`` / ``player_aaaa``、页面内
    ``m3u8/mp4`` 直链）。

已知边界：
1. ``tabs`` / ``lists`` 写成 ``js:`` 代码块时无法求值，退化到「结构化兜底」：
   把 ``.context`` 下每个含播放链接的 ``.widget`` 当成一条线路。6v 这类
   MacCMS 多线路主题正好是这种结构，实测能拿到 4 条线路。
2. 磁力线路（``magnet:``）直接跳过：播放器放不了磁力链。
3. 播放页只给了第三方 iframe（例如 ``vip.dytt-kan.com/share/...``）时无法嗅探，
   这类线路在详情阶段会被探针剔除，避免用户点到必然失败的线路。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import urllib.parse

import httpx

import runtime

KIND = "drpy"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_TIMEOUT = 12.0
_MAX_RULE = 200_000
_DIRECT_RE = re.compile(r"\.(?:m3u8|mp4|flv|mkv|mov|m4v)(?:$|[?#])", re.I)
_MAGNET_RE = re.compile(r"^(?:magnet:|ed2k:|thunder:)", re.I)


def enabled(source: dict | None) -> bool:
    """是否 drpy 源；只看 ``kind``，不看规则正文。

    路由（``catalog._routes``）里只残留 id/名称/api/kind，没有 ``rule``。
    如果这里再要求规则正文，从路由还原出的源会被判成 CMS 源，翻页去打
    ``ac=detail`` 拿到 HTML，解析成空页——列表变成 0 条、总数还被收敛成 0。
    红果侧一直只看 kind，这里与它保持一致；规则缺失由 ``rule_of`` 兜底。
    """
    if not source:
        return False
    return str(source.get("kind") or "").strip().lower() == KIND


def new_id(host: str, title: str = "") -> str:
    """同一份规则 + 站点永远得到同一个 id，重复录入会被识别成已存在。"""
    raw = f"{str(host or '').strip()}|{str(title or '').strip()}"
    return "dr_" + hashlib.md5(raw.encode()).hexdigest()[:10]


# ------------------------------------------------------------------ 规则解析
class JsRuleError(ValueError):
    pass


class _R:
    """JS 对象字面量解析器（只覆盖 drpy 源用到的子集）。"""

    def __init__(self, s: str):
        self.s = s
        self.i = 0
        self.n = len(s)

    def eof(self) -> bool:
        return self.i >= self.n

    def peek(self) -> str:
        return self.s[self.i] if self.i < self.n else ""

    def ws(self) -> None:
        while self.i < self.n:
            c = self.s[self.i]
            if c in " \t\r\n\ufeff":
                self.i += 1
            elif self.s.startswith("//", self.i):
                while self.i < self.n and self.s[self.i] not in "\r\n":
                    self.i += 1
            elif self.s.startswith("/*", self.i):
                j = self.s.find("*/", self.i + 2)
                self.i = self.n if j < 0 else j + 2
            else:
                return

    def _match_brace(self, open_ch: str, close_ch: str) -> str:
        """括号配对，跳过字符串 / 注释 / 正则字面量。

        正则字面量要靠「上一个 token 是不是操作数」判定，否则函数体里的
        ``/[^"']+/`` 会被当成字符串起点，配对立刻崩。
        """
        start = self.i
        depth = 0
        quote = ""
        esc = False
        prev_op = True
        while self.i < self.n:
            c = self.s[self.i]
            if quote:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == quote:
                    quote = ""
                    prev_op = False
                self.i += 1
                continue
            if self.s.startswith("//", self.i):
                j = self.s.find("\n", self.i)
                self.i = self.n if j < 0 else j + 1
                continue
            if self.s.startswith("/*", self.i):
                j = self.s.find("*/", self.i + 2)
                self.i = self.n if j < 0 else j + 2
                continue
            if c in "'\"`":
                quote = c
                self.i += 1
                continue
            if c == "/" and prev_op:
                # 正则字面量
                self.i += 1
                esc2 = False
                while self.i < self.n:
                    ch = self.s[self.i]
                    if esc2:
                        esc2 = False
                    elif ch == "\\":
                        esc2 = True
                    elif ch == "/":
                        self.i += 1
                        while self.i < self.n and self.s[self.i].isalpha():
                            self.i += 1
                        break
                    self.i += 1
                prev_op = False
                continue
            if c == open_ch:
                depth += 1
                prev_op = True
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return self.s[start:self.i]
                prev_op = False
            elif c.isalnum() or c in "_$":
                prev_op = False
            elif c in ")]}":
                prev_op = False
            else:
                prev_op = True
            self.i += 1
        raise JsRuleError(f"括号不配对：{open_ch}")

    def string(self) -> str:
        q = self.peek()
        self.i += 1
        out = []
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\":
                nxt = self.s[self.i:self.i + 2]
                mapping = {"\\n": "\n", "\\t": "\t", "\\r": "\r",
                           "\\'": "'", '\\"': '"', "\\`": "`", "\\\\": "\\"}
                if nxt in mapping:
                    out.append(mapping[nxt])
                    self.i += 2
                    continue
                if nxt == "\\u":
                    try:
                        out.append(chr(int(self.s[self.i + 2:self.i + 6], 16)))
                        self.i += 6
                        continue
                    except ValueError:
                        pass
                out.append(nxt[1:])
                self.i += 2
                continue
            if c == q:
                self.i += 1
                return "".join(out)
            out.append(c)
            self.i += 1
        raise JsRuleError("字符串没有结束")

    def key(self) -> str:
        self.ws()
        c = self.peek()
        if c in "'\"":
            return self.string()
        if c == "[":
            self._match_brace("[", "]")
            return ""
        start = self.i
        while self.i < self.n and (self.s[self.i].isalnum()
                                   or self.s[self.i] in "_$"):
            self.i += 1
        if start == self.i:
            raise JsRuleError(f"键名异常：{self.s[start:start + 20]!r}")
        return self.s[start:self.i]

    def value(self):
        self.ws()
        c = self.peek()
        if not c:
            raise JsRuleError("规则意外结束")
        if c in "'\"`":
            return self.string()
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c == "-" or c.isdigit():
            start = self.i
            self.i += 1
            while self.i < self.n and (self.s[self.i].isdigit()
                                       or self.s[self.i] in ".eE+-"):
                self.i += 1
            raw = self.s[start:self.i]
            try:
                return float(raw) if any(ch in raw for ch in ".eE") else int(raw)
            except ValueError:
                return raw
        start = self.i
        while self.i < self.n and (self.s[self.i].isalnum()
                                   or self.s[self.i] in "_$."):
            self.i += 1
        word = self.s[start:self.i]
        if word in ("true", "True"):
            return True
        if word in ("false", "False"):
            return False
        if word in ("null", "undefined", "None"):
            return None
        self.ws()
        # $js.toString(function(){...}) / 箭头函数 → 保留源码（我们不执行）
        if self.peek() == "(":
            src = self._match_brace("(", ")")
            return src[1:-1].strip()
        if word.startswith("function"):
            if self.peek() == "(":
                params = self._match_brace("(", ")")
                self.ws()
                body = self._match_brace("{", "}")
                return "function" + params + body
            return "function"
        return word

    def obj(self) -> dict:
        self.i += 1
        out: dict = {}
        while True:
            self.ws()
            if self.peek() == "}":
                self.i += 1
                return out
            if self.eof():
                raise JsRuleError("对象没有结束")
            k = self.key()
            self.ws()
            if self.peek() != ":":
                raise JsRuleError(f"缺少冒号：{self.s[self.i:self.i + 30]!r}")
            self.i += 1
            out[k] = self.value()
            self.ws()
            if self.peek() == ",":
                self.i += 1
                continue
            if self.peek() == "}":
                self.i += 1
                return out
            if self.eof():
                raise JsRuleError("对象没有结束")

    def arr(self) -> list:
        self.i += 1
        out: list = []
        while True:
            self.ws()
            if self.peek() == "]":
                self.i += 1
                return out
            out.append(self.value())
            self.ws()
            if self.peek() == ",":
                self.i += 1
                continue
            if self.peek() == "]":
                self.i += 1
                return out
            if self.eof():
                raise JsRuleError("数组没有结束")


def parse_rule(src: str) -> dict:
    """从整份 drpy 源里取出 ``var rule = {...}``。"""
    text = str(src or "")[:_MAX_RULE]
    idx = -1
    for marker in ("var rule", "let rule", "const rule", "rule =", "rule="):
        p = text.find(marker)
        if p >= 0:
            idx = p
            break
    if idx < 0:
        raise JsRuleError("未找到 rule 定义")
    eq = text.find("=", idx)
    if eq < 0:
        raise JsRuleError("rule 定义缺少 =")
    r = _R(text)
    r.i = eq + 1
    r.ws()
    if r.peek() != "{":
        raise JsRuleError("rule 不是对象字面量")
    rule = r.obj()
    if not isinstance(rule, dict) or not rule.get("host"):
        raise JsRuleError("rule 解析为空或缺少 host")
    return rule


_RULES: dict[str, tuple[dict, float]] = {}


def rule_of(source: dict | None) -> dict:
    """按规则文本缓存解析结果；源被编辑后签名变化自动重解析。"""
    text = str((source or {}).get("rule") or "").strip()
    if not text:
        return {}
    sig = hashlib.md5(text.encode()).hexdigest()[:16]
    hit = _RULES.get(sig)
    if hit and time.monotonic() - hit[1] < 3600:
        return hit[0]
    rule = parse_rule(text)
    _RULES[sig] = (rule, time.monotonic())
    if len(_RULES) > 32:
        for k, _ in sorted(_RULES.items(), key=lambda kv: kv[1][1])[:8]:
            _RULES.pop(k, None)
    return rule


# ------------------------------------------------------------------ 选择器
_ATTRS = {"text", "html", "href", "src", "title", "alt", "class", "id", "style",
          "content", "value", "type", "name", "datetime", "onclick", "data"}
_PSEUDO_RE = re.compile(r":(eq|gt|lt)\(\s*(-?\d+)\s*\)$|:(first|last)$"
                        r"|:contains\(([^)]*)\)$|:not\((:?contains\([^)]*\))\)$",
                        re.I)


def _parser(html: str):
    try:
        from selectolax.lexbor import LexborHTMLParser
    except ImportError as exc:  # pragma: no cover - 打包缺失时才发生
        raise JsRuleError("缺少 selectolax，无法解析 HTML") from exc
    return LexborHTMLParser(html or "<html></html>")


def _text(el) -> str:
    try:
        return (el.text() or "").strip()
    except Exception:  # noqa: BLE001 - 解析异常等价于空值
        return ""


def _apply_pseudo(nodes: list, expr: str) -> list:
    m = _PSEUDO_RE.match(expr)
    if not m:
        return nodes
    if m.group(1):
        n = int(m.group(2))
        if m.group(1).lower() == "eq":
            return nodes[n:n + 1] if n >= 0 else nodes[n:] or []
        if m.group(1).lower() == "gt":
            return nodes[n + 1:] if n >= 0 else nodes[:n]
        return nodes[:n] if n >= 0 else nodes[:len(nodes) + n]
    if m.group(3):
        return nodes[:1] if m.group(3).lower() == "first" else nodes[-1:]
    if m.group(4) is not None:
        needle = m.group(4)
        return [n for n in nodes if needle in _text(n)]
    if m.group(5):
        inner = m.group(5)
        needle = re.search(r"contains\(([^)]*)\)", inner)
        if needle:
            return [n for n in nodes if needle.group(1) not in _text(n)]
        return nodes
    return nodes


def _select_level(node, expr: str) -> list:
    expr = (expr or "").strip()
    if not expr or expr.lower() == "body":
        return [node] if node is not None else []
    pseudos: list[str] = []
    while True:
        m = _PSEUDO_RE.search(expr)
        if not m:
            break
        pseudos.insert(0, m.group(0))
        expr = expr[:m.start()].strip()
    nodes = [node] if not expr else list(node.css(expr))
    for p in pseudos:
        nodes = _apply_pseudo(nodes, p)
    return nodes


def _chain(node, expr: str) -> list:
    cur = [node]
    for part in str(expr or "").split("&&"):
        part = part.strip()
        if not part:
            continue
        nxt: list = []
        for c in cur:
            nxt.extend(_select_level(c, part))
        cur = nxt
        if not cur:
            return []
    return cur


def _split_rule(rule) -> tuple[list, str]:
    parts = [p for p in str(rule or "").split("&&") if p.strip()]
    attr = ""
    if len(parts) > 1:
        last = parts[-1].strip()
        low = last.lower()
        if low in _ATTRS or low.startswith("data-"):
            attr = parts.pop().strip()
    return parts, attr


def value(node, rule) -> str:
    """``sel&&attr`` → 单值；``body&&Text`` 表示节点自身。"""
    parts, attr = _split_rule(rule)
    nodes = [node]
    for p in parts:
        nxt: list = []
        for c in nodes:
            nxt.extend(_select_level(c, p))
        nodes = nxt
        if not nodes:
            return ""
    el = nodes[0]
    if not attr:
        return _text(el)
    low = attr.lower()
    if low == "text":
        return _text(el)
    if low == "html":
        return (getattr(el, "html", "") or "").strip()
    return (el.attributes.get(attr) or "").strip()


def pick(node, rule) -> str:
    """``;`` 分隔的多条兜底，返回第一个非空值。"""
    for cand in str(rule or "").split(";"):
        cand = cand.strip()
        if not cand or cand.lower().startswith("js:"):
            continue
        got = value(node, cand)
        if got:
            return got
    return ""


def _abs(base: str, href: str) -> str:
    href = str(href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    return urllib.parse.urljoin(base, href)


# ------------------------------------------------------------------ 规则求值
def _expand(tpl: str, cls: str = "", page: int = 1, keyword: str = "") -> str:
    """填 ``fyclass`` / ``fypage`` / ``fyfilter`` / ``**``，并处理 ``[首页地址]``。"""
    out = str(tpl or "")
    # ``[首页地址][后续页地址]``：方括号里是**完整**地址，不是片段。
    brackets = re.findall(r"\[([^\[\]]*)\]", out)
    if brackets:
        base = re.sub(r"\[[^\[\]]*\]", "", out)
        if page <= 1:
            out = brackets[0]
        elif len(brackets) > 1:
            out = brackets[1]
        else:
            out = base
    out = out.replace("fyfilter", "")
    out = out.replace("fyclass", urllib.parse.quote(str(cls), safe=""))
    out = out.replace("fypage", str(max(1, int(page))))
    if keyword:
        out = out.replace("**", urllib.parse.quote(str(keyword), safe=""))
    return out


def _headers(source: dict, rule: dict, referer: str = "") -> dict:
    heads = {"User-Agent": _UA}
    raw = rule.get("headers")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(k, str):
                continue
            if str(v).upper() == "MOBILE_UA":
                heads[k] = ("Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
            else:
                heads[k] = str(v)
    host = str(rule.get("host") or source.get("api") or "")
    heads.setdefault("Referer", referer or (host.rstrip("/") + "/"))
    return heads


def _timeout(rule: dict) -> float:
    try:
        ms = float(rule.get("timeout") or 0)
    except (TypeError, ValueError):
        ms = 0
    if ms <= 0:
        return _TIMEOUT
    return max(4.0, min(ms / 1000.0, 20.0))


async def _fetch(client: httpx.AsyncClient, url: str, headers: dict,
                 timeout: float, method: str = "GET", data=None) -> str:
    """带跳转的抓取。

    不能直接用 ``follow_redirects=True``：POST 搜索遇到 http→https 的 301 时
    httpx 会按标准把请求降级成不带 body 的 GET，源站就回「没有搜索到相关
    内容」。这里对「只是换 scheme/host、路径不变」的跳转保留方法与请求体，
    换路径的跳转才降级成 GET。
    """
    target = url
    for _ in range(4):
        try:
            resp = await client.request(method, target, headers=headers,
                                        data=data, timeout=timeout,
                                        follow_redirects=False)
        except Exception:  # noqa: BLE001 - 网络异常等价于空页面
            return ""
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location") or ""
            nxt = _abs(target, loc)
            if not nxt:
                return ""
            same_path = (urllib.parse.urlsplit(nxt).path
                         == urllib.parse.urlsplit(target).path)
            if resp.status_code in (307, 308) or same_path:
                target = nxt
                continue
            method, data = "GET", None
            target = nxt
            continue
        if resp.status_code >= 400:
            return ""
        try:
            return resp.text
        except Exception:  # noqa: BLE001 - 编码异常时按字节兜底
            return resp.content.decode("utf-8", "replace")
    return ""


def _list_rule(rule: dict, *keys) -> str:
    """取列表规则；``*`` 是 drpy 的「同 一级」占位。"""
    for key in keys:
        raw = rule.get(key)
        if isinstance(raw, str) and raw.strip() and raw.strip() != "*":
            return raw
    return ""


def _rows(html: str, rule: str, base: str,
          limit: int = 60) -> list[dict]:
    """列表规则 → ``[{title, poster, remark, url}]``。"""
    segs = [s for s in str(rule or "").split(";")]
    if not segs or not segs[0].strip():
        return []
    item_sel, fields = segs[0], [s.strip() for s in segs[1:] if s.strip()]
    tree = _parser(html)
    out: list[dict] = []
    for node in _chain(tree.root, item_sel)[:limit]:
        title = pick(node, fields[0]) if len(fields) > 0 else ""
        poster = pick(node, fields[1]) if len(fields) > 1 else ""
        remark = pick(node, fields[2]) if len(fields) > 2 else ""
        href = pick(node, fields[3]) if len(fields) > 3 else ""
        if not title or not href:
            continue
        url = _abs(base, href)
        if not url.startswith(("http://", "https://")):
            continue
        out.append({"title": title, "poster": _abs(base, poster),
                    "remark": remark, "url": url})
    return out


def _classes(html: str, rule: dict, base: str) -> list[dict]:
    raw = str(rule.get("class_parse") or "").strip()
    if not raw:
        return []
    segs = [s for s in raw.split(";")]
    tree = _parser(html)
    nodes = _chain(tree.root, segs[0])
    if not nodes:
        return []
    name_rule = segs[1] if len(segs) > 1 else ""
    href_rule = segs[2] if len(segs) > 2 else "a&&href"
    regex = segs[3] if len(segs) > 3 else ""
    exclude = str(rule.get("cate_exclude") or "").strip()
    out: list[dict] = []
    seen: set[str] = set()
    for node in nodes:
        name = pick(node, name_rule) if name_rule else _text(node)
        href = pick(node, href_rule)
        if not name or not href:
            continue
        if exclude:
            try:
                if re.search(exclude, name):
                    continue
            except re.error:
                pass
        cid = href
        if regex:
            try:
                m = re.search(regex, href)
            except re.error:
                m = None
            if m:
                cid = m.group(1) if m.groups() else m.group(0)
        cid = str(cid).strip().strip("/")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append({"id": cid, "name": name})
    return out[:40]


# ------------------------------------------------------------------ 详情
def _heading(node) -> str:
    for sel in ("h4", "h3", "h2", "b", "strong"):
        el = node.css_first(sel)
        if el:
            got = _text(el)
            if got:
                return got
    html = getattr(node, "html", "") or ""
    head = re.split(r"<a\b", html, maxsplit=1)[0]
    return " ".join(re.sub(r"<[^>]+>", " ", head).split())[:40]


def _episodes(node, selector: str, base: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    nodes = _chain(node, selector) if selector else []
    for a in nodes:
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith("#") or _MAGNET_RE.match(href):
            continue
        url = _abs(base, href)
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        out.append({"title": _text(a) or f"第{len(out) + 1}集", "url": url,
                    "direct": False, "headers": {}})
    return out


def _structural_lines(html: str, base: str) -> dict[str, list[dict]]:
    """``tabs``/``lists`` 是 ``js:`` 时的兜底。

    MacCMS 多线路主题把每条线路放在 ``.context`` 下独立的 ``.widget`` 里，
    第一个 ``<a>`` 之前的文本就是线路名。6v 的 4 条「播放地址」正是这个结构。
    """
    tree = _parser(html)
    scope = tree.css(".context .widget") or tree.css(".widget")
    lines: dict[str, list[dict]] = {}
    for node in scope:
        eps = _episodes(node, "a", base)
        if not eps:
            continue
        name = _heading(node) or f"线路{len(lines) + 1}"
        # 同一线路名重复时加后缀，避免把两条线路合成一条
        key = name
        n = 2
        while key in lines:
            key = f"{name}{n}"
            n += 1
        lines[key] = eps
    return lines


def _detail_lines(tree, rule: dict, base: str) -> dict[str, list[dict]]:
    tab_rule = rule.get("tabs")
    list_rule = rule.get("lists")
    # js: 代码块无法求值 → 结构化兜底
    if (isinstance(tab_rule, str) and tab_rule.strip().lower().startswith("js:")) \
            or (isinstance(list_rule, str) and list_rule.strip().lower().startswith("js:")):
        return _structural_lines(getattr(tree, "html", "") or "", base)
    if not isinstance(list_rule, str) or not list_rule.strip():
        return _structural_lines(getattr(tree, "html", "") or "", base)

    containers = [n for n in tree.css("[id^=playlist]")
                  if (n.attributes.get("id") or "").startswith("playlist")]
    containers = [c for c in containers if c.css("a")]
    # MacCMS 多播放源：把 #playlist1 换成每个容器，线路名取对应标题
    if len(containers) > 1 and "#playlist1" in list_rule:
        titles = [n for n in tree.css("h3, h2, .title")
                  if _text(n) and "播放" in _text(n)] if not isinstance(
            tab_rule, str) else []
        lines: dict[str, list[dict]] = {}
        for i, c in enumerate(containers):
            cid = c.attributes.get("id") or f"playlist{i + 1}"
            name = (_text(titles[i]) if i < len(titles) else "") or f"线路{i + 1}"
            eps = _episodes(c, list_rule.replace("#playlist1", f"#{cid}"), base)
            if eps:
                lines[name] = eps
        if lines:
            return lines

    name = ""
    if isinstance(tab_rule, str) and tab_rule.strip():
        name = pick(tree.root, tab_rule)
    eps = _episodes(tree.root, list_rule, base)
    return {name or "默认线路": eps} if eps else {}


async def detail(client: httpx.AsyncClient, source: dict,
                 vod_url: str) -> dict | None:
    rule = rule_of(source)
    if not rule or not vod_url:
        return None
    heads = _headers(source, rule, referer=_abs(rule.get("host"), "/"))
    html = await _fetch(client, vod_url, heads, _timeout(rule))
    if not html:
        return None
    tree = _parser(html)
    cfg = rule.get("二级")
    info = {"title": "", "cover": "", "desc": "", "content": "",
            "lines": {}}
    if isinstance(cfg, dict):
        info["title"] = pick(tree.root, cfg.get("title"))
        info["cover"] = _abs(vod_url, pick(tree.root, cfg.get("img")))
        info["desc"] = pick(tree.root, cfg.get("desc"))
        info["content"] = pick(tree.root, cfg.get("content"))
    elif isinstance(cfg, str):
        segs = cfg.split(";")
        info["title"] = pick(tree.root, segs[0] if segs else "")
        info["cover"] = _abs(vod_url, pick(tree.root, segs[1] if len(segs) > 1 else ""))
        info["desc"] = pick(tree.root, segs[2] if len(segs) > 2 else "")
    info["lines"] = _detail_lines(tree, cfg if isinstance(cfg, dict) else rule,
                                  vod_url)
    if not info["title"]:
        info["title"] = pick(tree.root, "h1&&Text;h2&&Text;title&&Text")
    return info


async def _probe_lines(client: httpx.AsyncClient, source: dict,
                       lines: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """用每线第 1 集探一次播放页，剔除只能靠 iframe 嗅探的死线路。

    6v 的 4 条「播放地址」里 2 条是第三方 iframe 分享页（服务端拿不到 m3u8），
    2 条是 Aliplayer 直出 m3u8。不探测就会让用户点到必然失败的线路。
    """
    if len(lines) <= 1:
        return lines
    names = list(lines)[:6]
    tasks = [asyncio.create_task(asyncio.wait_for(
        resolve(client, source, lines[n][0]["url"]), 6.0)) for n in names]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    alive: dict[str, list[dict]] = {}
    for name, got in zip(names, results):
        if isinstance(got, str) and got:
            eps = lines[name]
            eps[0] = {**eps[0], "url": got, "direct": True}
            alive[name] = eps
    for name in list(lines)[6:]:
        alive[name] = lines[name]
    # 全都探不出来时保留原样，让详情页至少能展示，播放时报明确错误
    return alive or lines


async def ensure_lines(client: httpx.AsyncClient, store, brief: dict) -> dict:
    """给分类/搜索结果补线路（列表项只有详情页地址，没有分集）。"""
    if not isinstance(brief, dict) or brief.get("lines"):
        return brief
    source = find_source(str(brief.get("provider") or ""))
    if not enabled(source):
        return brief
    data = await detail(client, source, str(brief.get("vod_id") or ""))
    if not data or not data.get("lines"):
        # 源站偶发返回空页/错误页（dbku 的详情页能拖到 10 秒以上），隔一下
        # 重试一次。不重试的后果是「搜索里点播放没反应」，用户得再点一遍。
        await asyncio.sleep(0.3)
        data = await detail(client, source, str(brief.get("vod_id") or ""))
    if not data or not data.get("lines"):
        runtime.log(f"drpy ensure: 无线路 {str(brief.get('vod_id'))[:60]}")
        return brief
    lines = await _probe_lines(client, source, data["lines"])
    updated = dict(brief)
    updated["lines"] = lines
    updated["line_names"] = list(lines)
    updated["line_sources"] = {name: str(source.get("id") or "") for name in lines}
    updated["episodes"] = max(len(eps) for eps in lines.values())
    updated["is_tv"] = updated["episodes"] > 1
    updated["primary_ext"] = "m3u8"
    for key, value in (("title", data.get("title")), ("poster", data.get("cover")),
                       ("overview", data.get("content") or data.get("desc"))):
        if value and (key == "title" or not updated.get(key)):
            updated[key] = str(value)
    try:
        store.put_brief(updated)
    except Exception as exc:  # noqa: BLE001 - 注册失败仍返回补好的 brief
        runtime.log(f"drpy put_brief failed: {type(exc).__name__}")
    runtime.log(f"drpy detail: {str(updated.get('title'))[:24]} "
                f"lines={list(lines)[:3]} eps={updated.get('episodes')}")
    return updated


# ------------------------------------------------------------------ 播放解析
_RESOLVED: dict[str, tuple[float, str]] = {}
_RESOLVE_TTL = 6 * 3600.0
_PLAYER_RE = re.compile(r"(?:player_data|player_aaaa|player_[a-z]+)\s*=\s*", re.I)
_MEDIA_RE = re.compile(
    r"""(?:https?:)?//[^"'\s<>\\]+?\.(?:m3u8|mp4|flv|mkv|mov|m4v)(?:\?[^"'\s<>\\]*)?(?=["'\s<>\\]|$)""",
    re.I)
_IFRAME_RE = re.compile(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", re.I)


def _decode_url(raw: str, encrypt) -> str:
    """MacCMS 播放地址的三种加密：0 明文 / 1 转义 / 2 base64+百分号编码。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if str(encrypt) in ("1", "true"):
        return urllib.parse.unquote(text)
    if str(encrypt) == "2":
        pad = "=" * (-len(text) % 4)
        try:
            import base64
            text = base64.b64decode(text + pad).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 不是合法 base64 就按原文
            pass
        return urllib.parse.unquote(text)
    return text


def _extract_media(html: str, base: str) -> str:
    text = str(html or "")
    if not text:
        return ""
    # 1) MacCMS 的 player_data / player_aaaa
    m = _PLAYER_RE.search(text)
    if m:
        start = text.find("{", m.end())
        if start >= 0:
            depth = 0
            quote = ""
            esc = False
            end = -1
            for i in range(start, len(text)):
                c = text[i]
                if esc:
                    esc = False
                    continue
                if c == "\\":
                    esc = True
                    continue
                if quote:
                    if c == quote:
                        quote = ""
                    continue
                if c in "\"'":
                    quote = c
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > 0:
                try:
                    data = json.loads(text[start:end])
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    got = _decode_url(data.get("url"), data.get("encrypt"))
                    got = _abs(base, got)
                    if got.startswith(("http://", "https://")) \
                            and _DIRECT_RE.search(got):
                        return got
                    if got.startswith(("http://", "https://")):
                        return ""
    # 2) 页面里直接写的 m3u8 / mp4（Aliplayer 的 source: "..."、video 标签等）
    for hit in _MEDIA_RE.finditer(text):
        got = _abs(base, hit.group(0))
        if got.startswith(("http://", "https://")):
            return got
    # 3) 只有第三方 iframe：需要浏览器嗅探，明说拿不到
    ifr = _IFRAME_RE.search(text)
    if ifr:
        runtime.log(f"drpy play: iframe only -> {ifr.group(1)[:90]}")
    return ""


async def resolve(client: httpx.AsyncClient, source: dict, url: str,
                  referer: str = "") -> str:
    """播放页地址 → 直链 m3u8/mp4；拿不到返回空串。"""
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return ""
    if _DIRECT_RE.search(url):
        return url
    sig = hashlib.md5(url.encode()).hexdigest()
    hit = _RESOLVED.get(sig)
    if hit and time.monotonic() - hit[0] < _RESOLVE_TTL:
        return hit[1]
    rule = rule_of(source)
    heads = _headers(source, rule, referer=referer)
    html = await _fetch(client, url, heads, _timeout(rule))
    out = _extract_media(html, url)
    if out:
        _RESOLVED[sig] = (time.monotonic(), out)
        if len(_RESOLVED) > 600:
            for k, _ in sorted(_RESOLVED.items(), key=lambda kv: kv[1][0])[:100]:
                _RESOLVED.pop(k, None)
    else:
        runtime.log(f"drpy play: unresolved {url[:90]}")
    return out


# ------------------------------------------------------------------ 对外接口
def _host(rule: dict, source: dict) -> str:
    host = str(rule.get("host") or source.get("api") or "").strip()
    if host and not host.startswith(("http://", "https://")):
        host = "https://" + host
    return host.rstrip("/")


async def probe(client: httpx.AsyncClient, source: dict) -> dict:
    """录入时校验：规则能解析、首页能打开、分类能抓到。"""
    out = {"ok": False, "message": "", "title": "", "classes": 0}
    try:
        rule = rule_of(source)
    except JsRuleError as exc:
        out["message"] = f"规则解析失败：{exc}"
        return out
    if not rule:
        out["message"] = "规则为空"
        return out
    out["title"] = str(rule.get("title") or "")
    host = _host(rule, source)
    if not host:
        out["message"] = "规则缺少 host"
        return out
    heads = _headers(source, rule)
    html = await _fetch(client, host + "/", heads, _timeout(rule))
    if not html:
        out["message"] = "站点打不开（可能被墙或需要代理）"
        return out
    rows = _classes(html, rule, host + "/")
    out["classes"] = len(rows)
    if not rows:
        # 分类抓不到仍然允许录入：有的站点首页结构与 class_parse 不匹配，
        # 但列表/详情/搜索仍可用，交给用户自己判断。
        out["message"] = "首页能打开，但没抓到分类"
        out["ok"] = True
        return out
    out["ok"] = True
    return out


async def classes(client: httpx.AsyncClient, source: dict) -> list[dict]:
    rule = rule_of(source)
    if not rule:
        return []
    host = _host(rule, source)
    html = await _fetch(client, host + "/", _headers(source, rule), _timeout(rule))
    return _classes(html, rule, host + "/")


def titles(source: dict, rows, provider_name: str = "") -> list:
    """drpy 列表项 → 本项目 ``cms.OnlineTitle``。"""
    import cms

    out = []
    for row in rows or []:
        out.append(cms.OnlineTitle(
            provider=str(source.get("id") or ""),
            vod_id=str(row.get("url") or ""),
            title=str(row.get("title") or ""),
            provider_name=provider_name or str(source.get("name") or "JS 源"),
            type_name="",
            year="",
            area="",
            remarks=str(row.get("remark") or ""),
            score="",
            poster=str(row.get("poster") or ""),
            overview="",
        ))
    return out


async def page_titles(client: httpx.AsyncClient, source: dict, tid: str,
                      page: int = 1) -> list:
    rule = rule_of(source)
    if not rule:
        return []
    host = _host(rule, source)
    path = _expand(str(rule.get("url") or ""), cls=tid, page=page)
    url = _abs(host + "/", path)
    if not url.startswith(("http://", "https://")):
        return []
    html = await _fetch(client, url, _headers(source, rule), _timeout(rule))
    rows = _rows(html, _list_rule(rule, "一级", "推荐"), url)
    return titles(source, rows)


async def search_titles(client: httpx.AsyncClient, source: dict,
                        keyword: str, page: int = 1) -> list:
    rule = rule_of(source)
    if not rule:
        return []
    host = _host(rule, source)
    tpl = str(rule.get("searchUrl") or "").strip()
    if not tpl:
        return []
    body = ""
    if "#" in tpl:
        tpl, body = tpl.split("#", 1)
    method = "GET"
    if body:
        body = body.replace("**", keyword)
        if body.endswith(";post"):
            body, method = body[:-5], "POST"
        elif not body.endswith(";get"):
            method = "POST"
    else:
        body = ""
    url = _abs(host + "/", _expand(tpl, keyword=keyword, page=page))
    heads = _headers(source, rule, referer=host + "/")
    if method == "POST":
        heads["Content-Type"] = "application/x-www-form-urlencoded"
    html = await _fetch(client, url, heads, _timeout(rule), method=method,
                        data=body.encode("utf-8") if body else None)
    if not html:
        # 6v 这套 MacCMS 的搜索入口是 /e/search/so.php，规则里写的 index.php
        # 会 404；同一个主题家族都是这个写法，顺手纠正一次。
        alt = url.replace("/e/search/index.php", "/e/search/so.php")
        if alt != url:
            html = await _fetch(client, alt, heads, _timeout(rule),
                                method=method,
                                data=body.encode("utf-8") if body else None)
            url = alt
    if not html:
        return []
    rows = _rows(html, _list_rule(rule, "搜索", "一级", "推荐"), url)
    return titles(source, rows, _clean_name(rule.get("title")))


def _clean_name(name) -> str:
    return re.sub(r"\[[^\]]*\]$", "", str(name or "")).strip()


def find_source(sid: str) -> dict | None:
    """按 id 取回完整源配置（规则文本只存在配置里）。"""
    import config

    for source in config.all_sources():
        if str(source.get("id") or "") == str(sid or ""):
            return source
    return None


def find_source_for_url(url: str) -> dict | None:
    """按分集地址的域名找一个能解析它的 drpy 源。

    CMS 采集站（如自建的 bd/bd12）经常直接把上游站点的播放页 URL 塞进
    ``vod_play_url``，而播放页要解析才能拿到 m3u8。启用着的 drpy 规则
    里若有同域名的（如 dbku.js 对应 www.dbku.tv），就可以借它解析。
    """
    import config

    host = urllib.parse.urlsplit(str(url or "")).netloc.lower()
    if not host:
        return None
    candidates = []
    for source in config.enabled_sources():
        if not enabled(source):
            continue
        try:
            rule_host = urllib.parse.urlsplit(
                _host(rule_of(source), source)).netloc.lower()
        except Exception:  # noqa: BLE001 - 坏规则跳过
            continue
        api_host = urllib.parse.urlsplit(str(source.get("api") or "")).netloc.lower()
        if host == rule_host or host == api_host:
            return source
        if rule_host and (host.endswith("." + rule_host)
                          or rule_host.endswith("." + host)):
            candidates.append(source)
    return candidates[0] if candidates else None
