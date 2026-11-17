#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
import urllib.parse
from collections import deque

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

import config
import netguard
import runtime

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
_URI_ATTR = re.compile(r'URI="([^"]+)"')

_AD_PLACEHOLDER = "__FNMEDIA_AD_SEGMENT__"
_AD_RANGE_RE = re.compile(r"(?i)\b(ad|ads|advert|advertisement|commercial)\b")
_AD_KEEP_TAGS = (
    "#EXTM3U", "#EXT-X-VERSION", "#EXT-X-TARGETDURATION",
    "#EXT-X-MEDIA-SEQUENCE", "#EXT-X-PLAYLIST-TYPE",
    "#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-START", "#EXT-X-ENDLIST",
    "#EXT-X-DISCONTINUITY-SEQUENCE", "#EXT-X-KEY", "#EXT-X-ALLOW-CACHE",
)
_AD_MAX_REMOVE_RATIO = 0.5
_AD_MAX_REMOVED = 40
# 固定 40 片在 2–3 秒切片的长清单里太紧：同一段 6 片贴片插几次就超了，
# 一旦触发整次回退，连片头广告也一起保留。按总片数给一个下限 + 比例上限。
_AD_MAX_REMOVED_SEG_RATIO = 0.20
_AD_MAX_BREAK_RATIO = 0.20
_AD_MAX_GROUPS = 10
_AD_MIN_GROUP_SEGMENTS = 3
_AD_MAX_GROUP_SEGMENTS = 15
_AD_MIN_GROUP_DURATION = 3.0
_AD_MAX_GROUP_DURATION = 120.0
# 片头贴片：清单最前面那一小段（1–6 片、<=90 秒），后面才是正文。
# 分段规则要求至少 3 片，单条 15/30 秒的贴片永远轮不到它，所以单独判定。
_AD_PREROLL_MAX_SEGMENTS = 6
# 有些源把正片切成 2 秒一片、每十几片一个 DISCONTINUITY，6 片只够 12 秒，
# 30/60 秒的贴片根本扫不到边界，所以按「边界 + 尾部补齐片」判定时放宽扫描窗口。
_AD_PREROLL_SCAN = 60
# 贴片被拼接进来时，最后一刀常常切不满一个切片长度（如 2s 切片里的 0.48s），
# 而正片自己的切段基本都是整长度。这是这类源唯一可靠的文本信号。
_AD_PREROLL_TAIL_SHORT = 0.6
# 中插拼接块：同一套「补齐片」特征出现在清单任何位置（贴片不一定在片头，
# 实测那个博彩贴片在片头、64 分钟、140 分钟各插了一次）。块长与占比都要小，
# 否则宁可不动。
_AD_PAD_MAX_DURATION = 90.0
_AD_PAD_MAX_RATIO = 0.05
# 外部目录拼接块：整组来自正文没出现过的流目录，且同一条流在清单里反复
# 出现（同一段贴片插很多次）。只出现一次的外部目录不够可靠，交给片头
# 探针和补齐片规则。
_AD_FOREIGN_MAX_RATIO = 0.05
_AD_FOREIGN_MAIN_SHARE = 0.50
_AD_PREROLL_MAX_DURATION = 90.0
_AD_PREROLL_MAX_RATIO = 0.20
_AD_PREROLL_MIN_SEGMENTS = 12
_AD_PREROLL_MIN_TOTAL = 180.0
_AD_PREROLL_MIN_TAIL = 90.0
_AD_PREROLL_SHAPE_GAP = 0.4
_AD_PREROLL_SHAPE_STRONG = 1.0
_AD_PREROLL_SHAPE_RESUME = 0.35
# 拼接证据：整条清单只有「片头 / 正文」两个分组，且边界落在片头范围内。
# 广告与正片常被同一套编码参数切出同样长度，时长形态上分不出来，只有拼接
# 边界可用；为避免误删正片自己的分段边界，还要求正文切片长度统一。
_AD_PREROLL_SPLICE_MAX_SEGMENTS = _AD_PREROLL_MAX_SEGMENTS
_AD_PREROLL_SPLICE_MIN_BODY = 24
_AD_PREROLL_SPLICE_TOLERANCE = 0.25
_AD_PREROLL_MAX_STREAMS = 2
_AD_MAX_GROUP_RELATIVE = 0.05
_AD_PREVIEWS: deque = deque(maxlen=5)


def _extinf_duration(line: str) -> float:
    stripped = line.strip()
    if not stripped.startswith("#EXTINF:"):
        return 0.0
    try:
        return max(0.0, float(stripped[8:].split(",", 1)[0]))
    except ValueError:
        return 0.0


def ad_rules() -> dict:
    cfg = config.get()
    keywords = cfg.get("adfilter_keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    try:
        head_skip = float(cfg.get("adfilter_head_skip") or 0)
    except (TypeError, ValueError):
        head_skip = 0.0
    return {
        "enabled": bool(cfg.get("adfilter_enabled")),
        "markers": bool(cfg.get("adfilter_markers", True)),
        "keywords": [str(word).strip().lower() for word in keywords[:32]
                     if str(word).strip()],
        "head_skip": max(0.0, min(120.0, head_skip)),
        "head_guard": bool(cfg.get("adfilter_head_guard", True)),
        "breaks": bool(cfg.get("adfilter_breaks", True)),
        "strong": bool(cfg.get("adfilter_strong", True)),
        "preroll": bool(cfg.get("adfilter_preroll", True)),
        "splices": bool(cfg.get("adfilter_splices", True)),
    }


def ad_rules_active(rules: dict) -> bool:
    return bool(rules["enabled"] and (
        rules["markers"] or rules["keywords"] or rules["head_skip"] > 0
        or rules["breaks"] or rules["strong"] or rules["preroll"]
        or rules["splices"]))


def _same_stream(left: str, right: str) -> bool:
    if not left or not right:
        return False
    a, b = urllib.parse.urlsplit(left), urllib.parse.urlsplit(right)
    if a.scheme != b.scheme or a.netloc != b.netloc:
        return False
    return a.path.rsplit("/", 1)[0] == b.path.rsplit("/", 1)[0]


def recent_ad_previews(limit: int = 3) -> list[dict]:
    return list(_AD_PREVIEWS)[-max(1, min(int(limit), 5)):]


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def _is_tracking_segment(url: str, duration: float) -> bool:
    if not 0 < duration < 0.5:
        return False
    path = urllib.parse.urlsplit(url).path.lower()
    return not path.endswith((".ts", ".m4s", ".mp4", ".m4a", ".aac", ".mp3"))


def _group_summary(records: list[dict]) -> dict[int, dict]:
    groups: dict[int, dict] = {}
    for rec in records:
        info = groups.setdefault(rec["group"], {"count": 0, "duration": 0.0})
        info["count"] += 1
        info["duration"] += rec["duration"]
    return groups


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    return (ordered[mid] if len(ordered) % 2
            else (ordered[mid - 1] + ordered[mid]) / 2)


def _stream_key(url: str) -> tuple[str, str, str]:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "", "", ""
    return parts.scheme, parts.netloc.lower(), parts.path.rsplit("/", 1)[0]


def _preroll_head_end(records: list[dict],
                      total_duration: float) -> tuple[int, str, str]:
    """片头贴片结束于第几段、依据哪种证据、没命中时的原因。

    三条证据，命中任意一条即可（外面还有时长/占比闸门兜底）：
    - ``discontinuity``：片头分组边界 + 时长形态同时成立；
    - ``splice``：整条清单只有两个分组、边界就在最前面几片，正文长度统一
      —— 广告和正片同名同时长时只有这条能抓到；
    - ``stream``：片头整段来自正文里没出现过的域名/目录，且要么时长形态成立
      要么就落在最前面几片；
    - ``tail``：片头这组以远短于正文切片的「补齐片」收尾（2 秒切片源里贴片
      最后一刀切不满），正文没有这种短片 —— 拼接块特征。
    没有边界时只接受更极端的时长突变（差 1 倍以上）。
    """
    limit = min(_AD_PREROLL_MAX_SEGMENTS, len(records) - 3)
    if limit < 1:
        return 0, "", "short"

    def body_median(end: int) -> float:
        return _median([rec["duration"] for rec in records[end:]
                        if rec["duration"] > 0])

    def head_deviation(end: int) -> float:
        durations = [rec["duration"] for rec in records[:end] if rec["duration"] > 0]
        median = body_median(end)
        if median <= 0 or not durations:
            return 0.0
        return abs(sum(durations) / len(durations) - median) / median

    def resumes_at(end: int) -> bool:
        if end >= len(records):
            return False
        duration, median = records[end]["duration"], body_median(end)
        return (duration > 0 and median > 0
                and abs(duration - median) / median <= _AD_PREROLL_SHAPE_RESUME)

    def shaped(end: int) -> bool:
        return head_deviation(end) >= _AD_PREROLL_SHAPE_GAP and resumes_at(end)

    def uniform_body(end: int) -> bool:
        values = [rec["duration"] for rec in records[end:] if rec["duration"] > 0]
        if len(values) < _AD_PREROLL_SPLICE_MIN_BODY:
            return False
        median = _median(values)
        if median <= 0:
            return False
        near = sum(1 for value in values
                   if abs(value - median) / median <= _AD_PREROLL_SPLICE_TOLERANCE)
        return near / len(values) >= 0.9

    def spliced(end: int) -> bool:
        if end > _AD_PREROLL_SPLICE_MAX_SEGMENTS:
            return False
        if len({rec["group"] for rec in records}) != 2:
            return False
        return uniform_body(end)

    def tail_spliced(end: int) -> bool:
        """片头这组以「补齐片」收尾：外部拼接块特征。

        2 秒切片的源里，正片每一组都切在整 2.000s，只有被拼进来的贴片
        最后一刀切不满，出现 0.48/0.96 这类短片。正文别处也可能有同样的
        补齐片（源站分块余量），所以这里只看片头这一组自身。
        """
        if end < 2 or end > _AD_PREROLL_SCAN:
            return False
        median = body_median(end)
        if median <= 0:
            return False
        threshold = median * _AD_PREROLL_TAIL_SHORT
        head_durations = [rec["duration"] for rec in records[:end - 1]
                          if rec["duration"] > 0]
        last = records[end - 1]["duration"]
        if not 0 < last < threshold:
            return False
        return all(duration >= threshold for duration in head_durations)

    streams = [_stream_key(rec["abs"]) for rec in records]
    scan = min(_AD_PREROLL_SCAN, len(records) - 3)
    miss = ""
    for index in range(1, scan + 1):
        if records[index]["group"] != records[0]["group"]:
            if shaped(index):
                return index, "discontinuity", ""
            if spliced(index):
                return index, "splice", ""
            if tail_spliced(index):
                return index, "tail", ""
            miss = "shape"
            break
    # 片头整段来自正文没出现过的域名/目录。从大到小取，把共享同一片头流的
    # 分片一次删干净（否则 index=1 会在第一片就返回，只删掉其中一片）。
    for index in range(limit, 0, -1):
        head_streams = set(streams[:index])
        if len(head_streams) > _AD_PREROLL_MAX_STREAMS:
            continue
        if head_streams & set(streams[index:]):
            continue
        if shaped(index) or index <= _AD_PREROLL_SPLICE_MAX_SEGMENTS:
            return index, "stream", ""
    if miss:
        return 0, "", miss
    # 没有边界时只看时长形态：开头几段异常、随后立刻回到正文统一时长。
    if len(records) < 24 or total_duration < _AD_PREROLL_MIN_TOTAL:
        return 0, "", "short"
    median = _median([rec["duration"] for rec in records[limit:]
                      if rec["duration"] > 0])
    if median <= 0:
        return 0, "", "duration"
    run = 0
    for rec in records[:limit]:
        duration = rec["duration"]
        if duration > 0 and abs(duration - median) / median >= _AD_PREROLL_SHAPE_STRONG:
            run += 1
        else:
            break
    if 1 <= run <= 3 and resumes_at(run):
        return run, "duration", ""
    return 0, "", "duration"


def _mark_preroll(records: list[dict], total_duration: float, stats: dict) -> None:
    """把片头贴片标成 ``preroll``；命中后其它规则不再重复计数。"""
    if len(records) < _AD_PREROLL_MIN_SEGMENTS or total_duration < _AD_PREROLL_MIN_TOTAL:
        stats["preroll_miss"] = "short"
        return
    end, why, miss = _preroll_head_end(records, total_duration)
    if not end:
        stats["preroll_miss"] = miss or "none"
        return
    head = records[:end]
    if any(rec["reason"] for rec in head):
        return  # 标记 / 关键字已经命中，交给更可靠的证据计数
    head_duration = sum(rec["duration"] for rec in head)
    if not (1.0 <= head_duration <= _AD_PREROLL_MAX_DURATION):
        stats["preroll_miss"] = "duration-gate"
        return
    if head_duration / total_duration > _AD_PREROLL_MAX_RATIO:
        stats["preroll_miss"] = "ratio"
        return
    if total_duration - head_duration < _AD_PREROLL_MIN_TAIL:
        stats["preroll_miss"] = "tail"
        return
    for rec in head:
        rec["reason"] = "preroll"
    stats["preroll"] = why


def _mark_padded_blocks(records: list[dict], total_duration: float,
                        stats: dict) -> None:
    """任意位置的拼接贴片：整组以「补齐片」收尾，就是外部拼进来的块。

    贴片不一定在片头 —— 实测那个博彩贴片在同一部片里插了三次。源站把
    正片切成整 2 秒一片，只有拼接块最后一刀切不满（0.48s 这类），这是
    它唯一留得下的文本痕迹。片尾最后一组本身就是收片补齐片，排除。
    """
    if len(records) < _AD_PREROLL_MIN_SEGMENTS or total_duration < _AD_PREROLL_MIN_TOTAL:
        return
    median = _median([rec["duration"] for rec in records if rec["duration"] > 0])
    if median <= 0:
        return
    threshold = median * _AD_PREROLL_TAIL_SHORT
    grouped: dict[int, list[dict]] = {}
    for rec in records:
        grouped.setdefault(rec["group"], []).append(rec)
    for group, block in grouped.items():
        if group == records[-1]["group"]:
            continue  # 片尾收片补齐片，不是贴片
        if len(block) < 2 or any(rec["reason"] for rec in block):
            continue
        if not 0 < block[-1]["duration"] < threshold:
            continue
        if any(rec["duration"] < threshold for rec in block[:-1]):
            continue
        duration = sum(rec["duration"] for rec in block)
        if not (1.0 <= duration <= _AD_PAD_MAX_DURATION):
            continue
        if duration / total_duration > _AD_PAD_MAX_RATIO:
            continue
        for rec in block:
            rec["reason"] = "pad"
        if len(stats["pad_blocks"]) < 12:
            stats["pad_blocks"].append({
                "group": group, "count": len(block),
                "duration": round(duration, 2),
            })


def _mark_foreign_blocks(records: list[dict], total_duration: float,
                         stats: dict) -> None:
    """整组来自正文之外的目录、且同一条流反复出现的短分组。

    与「补齐片」互补：源站把贴片切成整段（没有 0.48s 尾巴）时，只要广告
    分片来自另一个流目录、同一目录在片头/中插/片尾反复出现，仍可整组删除。
    要求重复出现是为了排除「正片中途换 CDN 目录」这类一次性切换；正文
    自身必须占绝对多数，外部流总占比也不超过 5%。
    """
    if len(records) < _AD_PREROLL_MIN_SEGMENTS or total_duration < _AD_PREROLL_MIN_TOTAL:
        return
    stream_durations: dict = {}
    for rec in records:
        key = _stream_key(rec["abs"])
        stream_durations[key] = (stream_durations.get(key, 0.0)
                                 + max(0.0, rec["duration"]))
    if len(stream_durations) < 2:
        return
    main_key, main_duration = max(stream_durations.items(), key=lambda item: item[1])
    if total_duration <= 0 or main_duration / total_duration < _AD_FOREIGN_MAIN_SHARE:
        return

    grouped: dict[int, list[dict]] = {}
    for rec in records:
        grouped.setdefault(rec["group"], []).append(rec)
    stream_groups: dict = {}
    for block in grouped.values():
        for key in {_stream_key(rec["abs"]) for rec in block}:
            stream_groups[key] = stream_groups.get(key, 0) + 1

    for group, block in grouped.items():
        if len(block) < 2 or any(rec["reason"] for rec in block):
            continue
        keys = {_stream_key(rec["abs"]) for rec in block}
        if not keys or main_key in keys:
            continue
        if any(stream_durations.get(key, 0.0) / total_duration > _AD_FOREIGN_MAX_RATIO
               for key in keys):
            continue
        if not all(stream_groups.get(key, 0) >= 2 for key in keys):
            continue
        duration = sum(rec["duration"] for rec in block)
        if not (_AD_MIN_GROUP_DURATION <= duration <= _AD_PAD_MAX_DURATION):
            continue
        if duration / total_duration > _AD_PAD_MAX_RATIO:
            continue
        for rec in block:
            rec["reason"] = "foreign"
        if len(stats["foreign_blocks"]) < 12:
            stats["foreign_blocks"].append({
                "group": group, "count": len(block),
                "duration": round(duration, 2),
            })


def strip_ads(text: str, base_url: str = "",
              rules: dict | None = None) -> tuple[str, dict]:
    """Drop ad segments from a media playlist, keeping AES-128 indexes intact.

    Removed segments leave a placeholder URI line so rewrite_playlist still
    advances its sequence counter; tag lines are dropped except global tags
    and #EXT-X-KEY, which must survive to keep the key scope of later segments.
    """
    rules = rules or ad_rules()
    stats = {"removed": 0, "removed_duration": 0.0, "kept_duration": 0.0,
             "total_duration": 0.0, "by_marker": 0, "by_keyword": 0,
             "by_head": 0, "by_break": 0, "by_duplicate": 0, "by_tiny": 0,
             "by_minority": 0, "by_preroll": 0, "by_pad": 0,
             "by_foreign": 0, "guard_kept": 0,
             "guard": "", "preroll": "", "preroll_miss": "", "head": [],
             "body_median": 0.0, "pad_blocks": [], "foreign_blocks": [],
             "sample": "", "dropped": [], "groups": [],
             "disc_cleaned": 0}
    lines = text.splitlines()
    total_duration = sum(_extinf_duration(row) for row in lines)
    stats["total_duration"] = total_duration
    if not ad_rules_active(rules) or is_master(text):
        stats["kept_duration"] = total_duration
        return text, stats

    head_active = rules["head_skip"] > 0
    if head_active and "#EXT-X-ENDLIST" not in text:
        head_active = False
        stats["guard"] = "head skip ignored: live playlist"

    records: list[dict] = []
    pending: list[str] = []
    cue_ad = False
    marker_hit = False
    elapsed = 0.0
    group = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            upper = stripped.upper()
            if upper == "#EXT-X-DISCONTINUITY":
                group += 1
            if rules["markers"]:
                if upper.startswith("#EXT-X-CUE-OUT"):
                    cue_ad = True
                elif upper.startswith("#EXT-X-CUE-IN"):
                    cue_ad = False
                elif upper.startswith("#EXT-X-DATERANGE") and _AD_RANGE_RE.search(stripped):
                    marker_hit = True
            pending.append(line)
            continue
        block, pending = pending, []
        duration = sum(_extinf_duration(row) for row in block)
        absolute = urllib.parse.urljoin(base_url, stripped) if base_url else stripped
        reason = ""
        if rules["markers"] and (cue_ad or marker_hit):
            reason = "marker"
        elif rules["keywords"] and any(word in absolute.lower()
                                       for word in rules["keywords"]):
            reason = "keyword"
        elif head_active and elapsed < rules["head_skip"]:
            reason = "head"
        elapsed += duration
        records.append({"tags": block, "uri": line, "abs": absolute,
                        "duration": duration, "reason": reason,
                        "group": group, "host": _host_of(absolute)})
        marker_hit = False
    tail = pending

    # 片头探针：无论最后删没删都留一份，管理页据此判断「为什么没删掉」。
    if records:
        reference = _stream_key(records[-1]["abs"])
        stats["head"] = [
            {"d": round(rec["duration"], 2), "g": rec["group"],
             "s": "same" if _stream_key(rec["abs"]) == reference else "diff"}
            for rec in records[:6]
        ]
        stats["body_median"] = round(_median(
            [rec["duration"] for rec in records if rec["duration"] > 0]), 2)

    # Strong evidence rules share one switch: all three are unconditional
    # inferences with the same false-positive risk, separate from grouping.
    if rules["strong"]:
        seen: set[str] = set()
        for rec in records:
            if rec["reason"]:
                continue
            if rec["abs"] in seen:
                rec["reason"] = "duplicate"
            elif _is_tracking_segment(rec["abs"], rec["duration"]):
                rec["reason"] = "tiny"
            seen.add(rec["abs"])

        # A host appearing at most twice in a long playlist is usually an
        # injected ad server, not a CDN mirror.
        if len(records) >= 20:
            counts: dict[str, int] = {}
            for rec in records:
                counts[rec["host"]] = counts.get(rec["host"], 0) + 1
            main_host = max(counts, key=counts.get) if counts else ""
            for rec in records:
                if (not rec["reason"] and rec["host"] != main_host
                        and counts[rec["host"]] <= 2):
                    rec["reason"] = "minority"

    # Conservative discontinuity-group heuristic: only short groups next to
    # much longer content groups, with several conditions all true.
    groups = _group_summary(records)
    stats["groups"] = [
        {"group": g, "count": info["count"],
         "duration": round(info["duration"], 2)}
        for g, info in sorted(groups.items())
    ]
    if rules["breaks"] and 2 <= len(groups) <= _AD_MAX_GROUPS:
        longest = max((info["duration"] for info in groups.values()), default=0.0)
        if longest > 0:
            for g, info in groups.items():
                if not (_AD_MIN_GROUP_SEGMENTS <= info["count"]
                        <= _AD_MAX_GROUP_SEGMENTS):
                    continue
                if not (_AD_MIN_GROUP_DURATION <= info["duration"]
                        <= _AD_MAX_GROUP_DURATION):
                    continue
                if info["duration"] / longest > _AD_MAX_GROUP_RELATIVE:
                    continue
                for rec in records:
                    if rec["group"] == g and not rec["reason"]:
                        rec["reason"] = "break"
            break_recs = [rec for rec in records if rec["reason"] == "break"]
            break_duration = sum(rec["duration"] for rec in break_recs)
            if (break_recs and total_duration > 0 and
                    break_duration / total_duration > _AD_MAX_BREAK_RATIO):
                for rec in break_recs:
                    rec["reason"] = ""
                stats["guard"] = "break groups exceed 20%, reverted"

    # A head-only hit that shares host and directory with kept content is
    # almost always the first real segment, not an ad break.
    # 但分组过密（>10 组，典型是 2 秒切片、每十几片一个 DISCONTINUITY 的源）
    # 时自动判断已经全部失效，这条保护会把手动设置的片头秒数也一并抵消，
    # 等于关掉了唯一可用的手段，所以只在分组稀疏、自动规则仍有效的清单上生效。
    if rules["head_guard"] and len(groups) <= _AD_MAX_GROUPS:
        has_feature = any(rec["reason"] in ("marker", "keyword")
                          for rec in records)
        if not has_feature:
            reference = next((rec["abs"] for rec in records
                              if rec["reason"] == ""), "")
            for rec in records:
                if rec["reason"] == "head" and _same_stream(reference, rec["abs"]):
                    rec["reason"] = ""
                    stats["guard_kept"] += 1
            if stats["guard_kept"] and not stats["guard"]:
                stats["guard"] = "head-only suspect segments kept"

    # 片头贴片兜底：单独一条 15/30 秒的贴片凑不满分段规则的最少 3 片。
    if rules["preroll"]:
        _mark_preroll(records, total_duration, stats)
    # 中插拼接贴片：同一特征（整组以补齐片收尾）出现在清单任意位置。
    if rules["splices"]:
        _mark_padded_blocks(records, total_duration, stats)
        # 没有补齐片尾巴、但整组来自反复出现的外部流目录的贴片。
        _mark_foreign_blocks(records, total_duration, stats)

    removed = [rec for rec in records if rec["reason"]]
    removed_duration = sum(rec["duration"] for rec in removed)
    max_removed = max(_AD_MAX_REMOVED,
                      int(len(records) * _AD_MAX_REMOVED_SEG_RATIO))
    if removed and (len(removed) > max_removed or
                    (total_duration > 0 and
                     removed_duration / total_duration > _AD_MAX_REMOVE_RATIO)
                    or len(removed) == len(records)):
        for rec in removed:
            rec["reason"] = ""
        stats["guard"] = "bulk removal reverted"
        stats["guard_kept"] += len(removed)
        removed = []

    if not removed:
        stats["kept_duration"] = total_duration
        return text, stats

    out: list[str] = []
    for index, rec in enumerate(records):
        if rec["reason"]:
            stats["removed"] += 1
            stats["removed_duration"] += rec["duration"]
            stats["by_" + rec["reason"]] += 1
            if not stats["sample"]:
                stats["sample"] = rec["abs"][:100]
            if len(stats["dropped"]) < 20:
                stats["dropped"].append({
                    "url": rec["abs"][:160],
                    "duration": round(rec["duration"], 2),
                    "reason": rec["reason"],
                    "group": rec["group"],
                })
            out.extend(row for row in rec["tags"]
                       if row.strip().upper().startswith(_AD_KEEP_TAGS))
            out.append(_AD_PLACEHOLDER)
        else:
            stats["kept_duration"] += rec["duration"]
            prev_removed = index > 0 and bool(records[index - 1]["reason"])
            tags = []
            for row in rec["tags"]:
                upper = row.strip().upper()
                if (upper.startswith("#EXT-X-CUE-")
                        or upper.startswith("#EXT-X-DATERANGE")):
                    continue
                # A discontinuity right after a removed ad group is an ad
                # exit marker; keeping it makes players re-init decoders.
                if upper == "#EXT-X-DISCONTINUITY" and prev_removed:
                    stats["disc_cleaned"] += 1
                    continue
                tags.append(row)
            out.extend(tags)
            out.append(rec["uri"])
    out.extend(tail)
    return "\n".join(out) + "\n", stats


def encode_url(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def decode_url(token: str) -> str:
    padded = token + "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(padded).decode()


_OPENSSL = shutil.which("openssl")


def can_decrypt() -> bool:
    return bool(_OPENSSL)


def encode_seg(url: str, key_url: str = "", iv: str = "") -> str:
    if not key_url:
        return encode_url(url)
    raw = json.dumps({"u": url, "k": key_url, "iv": iv}, separators=(",", ":"))
    return "k" + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_seg(token: str) -> tuple[str, str, str]:
    if not token.startswith("k"):
        return decode_url(token), "", ""
    padded = token[1:] + "=" * (-len(token[1:]) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded).decode())
    return str(data.get("u", "")), str(data.get("k", "")), str(data.get("iv", ""))


_KEYS: dict = {}
_KEY_TTL = 120.0


async def fetch_key(client, url: str, timeout: float = 8.0) -> bytes:
    now = time.monotonic()
    hit = _KEYS.get(url)
    if hit and now - hit[0] < _KEY_TTL:
        return hit[1]
    resp = await client.get(url, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    _KEYS[url] = (now, resp.content)
    if len(_KEYS) > 300:
        for stale in list(_KEYS)[:60]:
            _KEYS.pop(stale, None)
    return resp.content


async def fetch_media_bytes(client, url: str, timeout: float = 15.0) -> bytes:
    parsed = urllib.parse.urlparse(url)
    resp = await client.get(url, timeout=timeout, headers={
        "User-Agent": _UA,
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
    })
    resp.raise_for_status()
    return resp.content


def decrypt_segment(data: bytes, key: bytes, iv: str) -> bytes | None:
    if not _OPENSSL or len(key) != 16:
        return None
    iv_hex = (iv or "0" * 32).removeprefix("0x").ljust(32, "0")[:32]
    try:
        proc = subprocess.run(
            [_OPENSSL, "enc", "-d", "-aes-128-cbc", "-K", key.hex(),
             "-iv", iv_hex],
            input=data, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def is_playlist(text: str) -> bool:
    head = (text or "").lstrip()[:64]
    return head.startswith("#EXTM3U") or "#EXTINF" in (text or "")[:2048]


_M3U8_RE = re.compile(r'''(?:https?:)?//[^\s"'<>\\]+?\.m3u8[^\s"'<>\\]*''')
_REL_M3U8_RE = re.compile(r'''["']([^"']+?\.m3u8[^"']*)["']''')


def extract_m3u8(text: str) -> str:
    """分享播放页（HTML/JSON）里硬编码的 m3u8 地址。"""
    plain = (text or "").replace("\\/", "/")
    for pattern in (_M3U8_RE, _REL_M3U8_RE):
        match = pattern.search(plain)
        if match:
            return match.group(1) if pattern is _REL_M3U8_RE else match.group(0)
    return ""


def ensure_public_target(url: str) -> str:
    """分片、图片与直链代理的目标校验。

    默认允许局域网 / 回环地址以内网自建源；可在设置中关闭
    allow_intranet 恢复仅公网目标。链路本地与保留地址始终拦截。
    """
    allow_intranet = bool(config.get().get("allow_intranet", True))
    return netguard.validate_target(url, allow_intranet=allow_intranet,
                                    label="媒体地址")


def parse_stream_inf(text: str) -> list[dict]:
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = {}
        for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', stripped):
            attrs[match.group(1)] = match.group(2).strip('"')
        uri = ""
        for nxt in lines[i + 1:]:
            candidate = nxt.strip()
            if not candidate:
                continue
            if not candidate.startswith("#"):
                uri = candidate
            break
        width = height = 0
        res = attrs.get("RESOLUTION", "")
        if "x" in res:
            try:
                width, height = int(res.split("x", 1)[0]), int(res.split("x", 1)[1])
            except ValueError:
                width = height = 0
        try:
            bandwidth = int(attrs.get("BANDWIDTH", "0") or 0)
        except ValueError:
            bandwidth = 0
        out.append({"uri": uri, "bandwidth": bandwidth,
                    "width": width, "height": height})
    return out


def build_playlist_meta(text: str, child: dict | None = None) -> dict:
    meta = {"width": 0, "height": 0, "bandwidth": 0, "variant_count": 0,
            "duration": 0.0, "has_endlist": True}
    meta.update({k: v for k, v in (child or {}).items() if v is not None})
    if is_master(text):
        variants = parse_stream_inf(text)
        meta["variant_count"] = len(variants)
        best = max(variants, key=lambda v: v["bandwidth"], default=None)
        if best:
            meta["bandwidth"] = best["bandwidth"]
            if best["width"] and best["height"]:
                meta["width"] = max(best["width"], best["height"])
                meta["height"] = min(best["width"], best["height"])
    else:
        duration = 0.0
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#EXTINF:"):
                try:
                    duration += float(stripped[8:].split(",")[0])
                except ValueError:
                    pass
        meta["duration"] = duration
        meta["has_endlist"] = "#EXT-X-ENDLIST" in text
    return meta


_PLAYLISTS: dict = {}
_PLAYLIST_TTL = 120.0


def remember_playlist_text(key: tuple, text: str, base: str, meta: dict) -> None:
    _PLAYLISTS[key] = (time.monotonic(), text, base, meta)
    if len(_PLAYLISTS) > 400:
        for stale in list(_PLAYLISTS)[:80]:
            _PLAYLISTS.pop(stale, None)


def get_playlist_text(key: tuple) -> tuple[str, str, dict] | None:
    hit = _PLAYLISTS.get(key)
    if not hit:
        return None
    ts, text, base, meta = hit
    if time.monotonic() - ts > _PLAYLIST_TTL:
        _PLAYLISTS.pop(key, None)
        return None
    return text, base, meta


_META: dict = {}
_META_TTL = 120.0


def remember_playlist_meta(key: tuple, meta: dict) -> None:
    _META[key] = (time.monotonic(), meta)
    if len(_META) > 500:
        for stale in list(_META)[:100]:
            _META.pop(stale, None)


def get_playlist_meta(key: tuple) -> dict | None:
    hit = _META.get(key)
    if not hit:
        return None
    ts, meta = hit
    if time.monotonic() - ts > _META_TTL:
        _META.pop(key, None)
        return None
    return meta


def pick_best_variant(text: str) -> str | None:
    variants = [v for v in parse_stream_inf(text) if v["uri"]]
    if not variants:
        return None
    return max(variants, key=lambda v: v["bandwidth"])["uri"]


def rewrite_playlist(text: str, base_url: str, seg_prefix: str,
                     variant_prefix: str = "", is_master: bool = False,
                     decrypt_keys: bool = False,
                     variant_query: str = "") -> str:
    if not is_master and ad_rules_active(ad_rules()):
        original = text
        text, ad_stats = strip_ads(text, base_url)
        if ad_stats["removed"] or ad_stats["guard_kept"] or ad_stats["guard"]:
            runtime.log(
                f"adfilter: removed={ad_stats['removed']} "
                f"cut={ad_stats['removed_duration']:.0f}s "
                f"kept={ad_stats['kept_duration']:.0f}s "
                f"total={ad_stats['total_duration']:.0f}s "
                f"hits=m{ad_stats['by_marker']}/k{ad_stats['by_keyword']}/"
                f"h{ad_stats['by_head']}/b{ad_stats['by_break']}/"
                f"d{ad_stats['by_duplicate']}/t{ad_stats['by_tiny']}/"
                f"o{ad_stats['by_minority']}/p{ad_stats['by_preroll']}/"
                f"s{ad_stats['by_pad']}/x{ad_stats['by_foreign']} "
                f"guard={ad_stats['guard'] or '-'} "
                f"guarded={ad_stats['guard_kept']} "
                f"preroll={ad_stats['preroll'] or '-'} "
                f"disc={ad_stats['disc_cleaned']} "
                f"sample={ad_stats['sample'][:80]}")
        # 不管删没删都留一条：没删掉的清单才是排查时最需要看的。
        _AD_PREVIEWS.append({
            "ts": time.time(),
            "removed": ad_stats["removed"],
            "removed_duration": round(ad_stats["removed_duration"], 2),
            "kept_duration": round(ad_stats["kept_duration"], 2),
            "total_duration": round(ad_stats["total_duration"], 2),
            "by_marker": ad_stats["by_marker"],
            "by_keyword": ad_stats["by_keyword"],
            "by_head": ad_stats["by_head"],
            "by_break": ad_stats["by_break"],
            "by_duplicate": ad_stats["by_duplicate"],
            "by_tiny": ad_stats["by_tiny"],
            "by_minority": ad_stats["by_minority"],
            "by_preroll": ad_stats["by_preroll"],
            "by_pad": ad_stats["by_pad"],
            "by_foreign": ad_stats["by_foreign"],
            "pad_blocks": ad_stats["pad_blocks"],
            "foreign_blocks": ad_stats["foreign_blocks"],
            "preroll": ad_stats["preroll"],
            "preroll_miss": ad_stats["preroll_miss"],
            "head": ad_stats["head"],
            "body_median": ad_stats["body_median"],
            "groups": ad_stats["groups"],
            "disc_cleaned": ad_stats["disc_cleaned"],
            "guard": ad_stats["guard"],
            "guard_kept": ad_stats["guard_kept"],
            "sample": ad_stats["sample"],
            "dropped": ad_stats["dropped"],
            "raw_head": "\n".join(original.splitlines()[:40]),
            "kept_head": "\n".join(text.splitlines()[:40]),
        })
    out = []
    key_url = ""
    key_iv = ""
    seq = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if not is_master and stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                seq = 0
        if not is_master and decrypt_keys and stripped.startswith("#EXT-X-KEY"):
            upper = stripped.upper()
            uri = _URI_ATTR.search(stripped)
            if "METHOD=AES-128" in upper and uri:
                key_url = urllib.parse.urljoin(base_url, uri.group(1))
                iv = re.search(r"IV=0x([0-9A-Fa-f]+)", stripped)
                key_iv = iv.group(1) if iv else ""
                continue
            key_url = ""
            key_iv = ""
        if stripped.startswith("#"):
            attr_target = variant_prefix if is_master else seg_prefix
            extra = variant_query if is_master else ""

            def sub(match):
                absolute = urllib.parse.urljoin(base_url, match.group(1))
                return (f'URI="{attr_target}?u={encode_url(absolute)}'
                        f'{extra}"')
            out.append(_URI_ATTR.sub(sub, line))
            continue
        if stripped == _AD_PLACEHOLDER:
            seq += 1
            continue
        target = variant_prefix if is_master else seg_prefix
        absolute = urllib.parse.urljoin(base_url, stripped)
        extra = variant_query if is_master else ""
        if key_url:
            iv = key_iv or f"{seq:032x}"
            out.append(f"{target}?u={encode_seg(absolute, key_url, iv)}{extra}")
        else:
            out.append(f"{target}?u={encode_url(absolute)}{extra}")
        seq += 1
    return "\n".join(out) + "\n"


async def fetch_playlist(client: httpx.AsyncClient, url: str,
                         timeout: float = 3.0,
                         depth: int = 0) -> tuple[str, str, dict]:
    parsed = urllib.parse.urlparse(url)
    headers = {"User-Agent": _UA,
               "Referer": f"{parsed.scheme}://{parsed.netloc}/"}
    resp = await client.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    text = resp.text
    base = str(resp.url)
    if not is_playlist(text):
        if depth < 2:
            child = extract_m3u8(text)
            if child:
                return await fetch_playlist(
                    client, ensure_public_target(urllib.parse.urljoin(base, child)),
                    timeout, depth + 1)
        raise ValueError("not a playlist")
    if is_master(text) and depth < 2:
        best = pick_best_variant(text)
        if best:
            child_text, child_base, child_meta = await fetch_playlist(
                client, urllib.parse.urljoin(base, best), timeout, depth + 1)
            return child_text, child_base, build_playlist_meta(text, child_meta)
    meta = build_playlist_meta(text)
    rules = ad_rules()
    if ad_rules_active(rules) and not is_master(text):
        _, ad_stats = strip_ads(text, base, rules)
        meta["duration"] = ad_stats["kept_duration"]
    return text, base, meta


def inject_start(text: str, offset: float) -> str:
    """在媒体清单头部声明起播点。

    手机/电视端的原生播放器不一定自己 seek，只按服务端下发的播放地址起播；
    官方转码会话返回的地址本身就带起播点，我们下发的是完整点播清单，因此
    用 HLS 标准的 EXT-X-START 补一个。直播窗口没有稳定时间轴，跳过。
    """
    if offset <= 0 or "#EXT-X-STREAM-INF" in text or "#EXT-X-ENDLIST" not in text:
        return text
    lines = text.splitlines()
    if not lines or not lines[0].strip().startswith("#EXTM3U"):
        return text
    tag = f"#EXT-X-START:TIME-OFFSET={offset:.3f},PRECISE=NO"
    return "\n".join([lines[0], tag] + lines[1:]) + "\n"


def playlist_response(text: str, base: str, seg_prefix: str,
                      variant_prefix: str = "",
                      is_master: bool = False,
                      decrypt_keys: bool = False,
                      start_offset: float = 0.0) -> Response:
    variant_query = ""
    if is_master and start_offset > 0:
        # master 不能带 EXT-X-START，把起播点透传给子清单。
        variant_query = f"&start={int(start_offset)}"
    body = rewrite_playlist(text, base, seg_prefix, variant_prefix,
                            is_master, decrypt_keys, variant_query)
    if start_offset > 0:
        body = inject_start(body, start_offset)
    return Response(body,
                    media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


async def proxy_media(request: Request, url: str, client: httpx.AsyncClient,
                      timeout: float = 8.0,
                      on_headers=None) -> StreamingResponse | Response:
    headers = {"User-Agent": _UA}
    for key in ("range", "if-range", "if-none-match"):
        if key in request.headers:
            headers[key] = request.headers[key]
    parsed = urllib.parse.urlparse(url)
    # 不能把播放器（我们自己的 NAS 地址）的 Referer 透传给源站，
    # 分享页的密钥/分片服务器会因此返回无效内容。
    headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    resp = await client.send(client.build_request("GET", url, headers=headers),
                             stream=True)
    out_headers = {}
    for key in ("content-type", "content-range", "accept-ranges", "etag"):
        if key in resp.headers:
            out_headers[key] = resp.headers[key]
    out_headers["Cache-Control"] = "public, max-age=3600"
    status = resp.status_code
    if status >= 400:
        await resp.aclose()
        return Response(status_code=status, headers=out_headers)
    # 上游自报的媒体属性（红果的 x-stream-quality / x-stream-codec）交给
    # 适配层缓存；回调失败绝不能影响播放。
    if on_headers:
        try:
            on_headers(dict(resp.headers))
        except Exception:  # noqa: BLE001 - 观测信息失败不算播放失败
            pass

    async def chunks():
        try:
            async for block in resp.aiter_bytes(65536):
                yield block
        finally:
            await resp.aclose()

    return StreamingResponse(chunks(), status_code=status, headers=out_headers,
                             media_type=out_headers.get("content-type"))


# --------------------------------------------------------------- 分片直连
_REDIRECT_PROBE: dict[str, tuple[float, bool, str]] = {}
_REDIRECT_SEEN: dict[str, tuple[float, int]] = {}
_PROBE_OK_TTL = 600.0
_PROBE_FAIL_TTL = 120.0
_RETRY_WINDOW = 5.0


def _private_target(url: str) -> bool:
    """内网 / 回环目标不能交给播放器直连：播放器未必在 NAS 网段。"""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").strip("[]")
    if not host:
        return True
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addrs = netguard._addresses(host, port)  # noqa: SLF001 - 同项目复用
    except OSError:
        return True
    return any(addr.is_private or addr.is_loopback for addr in addrs)


async def _probe_direct(client: httpx.AsyncClient,
                        url: str) -> tuple[bool, str]:
    """模拟播放器直连：不带 Referer，HEAD 不通再 Range 取 1 字节。

    源站靠 Referer 防盗链时这里会拿到 403，判定为「不能 302」；
    跟随源站自身重定向后返回最终地址，播放器少跳一次。
    """
    try:
        resp = await client.head(url, headers={"User-Agent": _UA}, timeout=3.0)
        return resp.status_code in (200, 206), str(resp.url)
    except Exception:  # noqa: BLE001 - HEAD 不被支持时换 Range GET
        pass
    try:
        req = client.build_request(
            "GET", url, headers={"User-Agent": _UA, "Range": "bytes=0-0"})
        resp = await client.send(req, stream=True)
        try:
            return resp.status_code in (200, 206), str(resp.url)
        finally:
            await resp.aclose()
    except Exception:  # noqa: BLE001 - 探测失败一律回退代理
        return False, url


async def redirect_target(token: str, url: str, client,
                          key_url: str = "", acl_on: bool = False) -> str:
    """能直连就返回 Location；不能则返回空串，调用方走原来的代理逻辑。

    判定必须在返回 302 **之前**完成：302 之后请求不再回到本服务，
    没有失败信号可用来「再解析返回」。
    """
    cfg = config.get()
    if not cfg.get("seg_redirect", True) or key_url or acl_on:
        return ""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    blocked = [str(x).lower() for x in
               (cfg.get("seg_redirect_block_hosts") or []) if str(x).strip()]
    if host in blocked or any(host.endswith("." + item) for item in blocked):
        return ""
    now = time.monotonic()
    # 重试兜底：302 失败后播放器会重新请求同一个分片 URL；第二次起强制
    # 代理，并把该域名标记为不可直连，避免后续分片继续踩坑。
    seen_ts, seen_n = _REDIRECT_SEEN.get(token, (0.0, 0))
    if now - seen_ts < _RETRY_WINDOW:
        if seen_n >= 1:
            _REDIRECT_PROBE[host] = (now, False, "")
            return ""
        _REDIRECT_SEEN[token] = (seen_ts, seen_n + 1)
    else:
        _REDIRECT_SEEN[token] = (now, 1)
    if len(_REDIRECT_SEEN) > 512:
        _REDIRECT_SEEN.clear()
    hit = _REDIRECT_PROBE.get(host)
    ttl = _PROBE_OK_TTL if hit and hit[1] else _PROBE_FAIL_TTL
    if hit and now - hit[0] < ttl:
        return hit[2] if hit[1] else ""
    if _private_target(url):
        _REDIRECT_PROBE[host] = (now, False, "")
        return ""
    ok, final = await _probe_direct(client, url)
    _REDIRECT_PROBE[host] = (now, ok, final if ok else "")
    runtime.log(f"seg redirect probe: host={host} ok={int(ok)}")
    return final if ok else ""


def _poster_fallback() -> Response:
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="3">'
           b'<rect width="2" height="3" fill="#1b2130"/></svg>')
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=600"})


async def proxy_poster(url: str, client: httpx.AsyncClient) -> Response:
    parsed = urllib.parse.urlparse(url)
    headers = {"User-Agent": _UA}
    host = parsed.netloc.lower()
    if host.endswith("doubanio.com") or host.endswith("douban.com"):
        headers["Referer"] = "https://movie.douban.com/"
    else:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    try:
        resp = await client.get(url, headers=headers, timeout=5.0)
        if resp.status_code != 200 or not resp.content:
            return _poster_fallback()
        head = resp.content[:12]
        if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            ctype = "image/webp"
        elif head[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
        elif head[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif head[:6] in (b"GIF87a", b"GIF89a"):
            ctype = "image/gif"
        else:
            declared = (resp.headers.get("content-type") or "").lower()
            ctype = declared if declared.startswith("image/") else "image/jpeg"
        return Response(resp.content, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400",
                                 "Access-Control-Allow-Origin": "*"})
    except Exception:
        return _poster_fallback()
