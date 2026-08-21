# -*- coding: utf-8 -*-
"""判分器注册表。

每个判分器签名统一为 check(response: str, spec: dict) -> (score: float, detail: str)

score 在 [0,1] 之间：
  - 通过/失败型返回 1.0 或 0.0
  - 得分点型（checkpoints）返回 命中数/总数，支持部分得分

之所以用连续分而不是布尔值：题库里大量题目本来就是 x/8、x/24 这种形式，
强行二值化会丢掉最有用的那部分信息（差一点 vs 差很多）。

返回 None 表示**本次不产生分数**（如 length 这类纯观测题）。它与 0 分
是两回事：0 分会拉低维度分，None 不参与任何统计。混淆这两者会让
「判不了」看起来像「答错了」。

人工判分已整体移除，见 bank.py 顶部。这里保留一道显式的护栏：
真的有 manual 走到这里，说明有人绕过了 bank.load()，要立刻炸掉而不是静默。
"""

import json
import math
import os
import re
import sys

CJK = re.compile(r"[一-鿿]")
FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+\-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)
NUM = re.compile(r"-?\d+(?:\.\d+)?")
TRAILING = "。．.!！?？，,、;；:：\"'“”‘’《》 \t\r\n"

_REGISTRY = {}


def checker(name):
    def deco(fn):
        _REGISTRY[name] = fn
        return fn
    return deco


class CheckerError(Exception):
    pass


def run_check(response, spec):
    """按 spec["type"] 分发，返回 (score, detail)。

    score 为 None 表示本次不计分（纯观测题）。
    判分器内部异常记 0 分并写明原因——一道脏输出不该炸掉整轮，
    但也不能悄悄当成没发生。
    """
    kind = (spec or {}).get("type")
    if not kind:
        raise CheckerError("check.type 缺失。题库加载时本应拦截，"
                           "走到这里说明有人绕过了 bank.load()")
    if kind == "manual":
        raise CheckerError("本工具不支持人工判分，见 bank.py")
    fn = _REGISTRY.get(kind)
    if fn is None:
        raise CheckerError("未知判分类型: %r。可用: %s"
                           % (kind, ", ".join(available())))
    try:
        return fn(response or "", spec)
    except Exception as e:                       # 判分器不该因脏输出崩掉整轮
        return 0.0, "判分异常: %s: %s" % (type(e).__name__, e)


def available():
    return sorted(_REGISTRY)


# --------------------------------------------------------------------------
# 文本归一化
# --------------------------------------------------------------------------

def strip_fence(t):
    m = FENCE.match((t or "").strip())
    return m.group(1) if m else (t or "")


def to_halfwidth(t):
    out = []
    for ch in t:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif o == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def norm(t):
    t = to_halfwidth(strip_fence(t))
    t = re.sub(r"\s+", " ", t).strip()
    return t.strip(TRAILING)


def cn_count(t):
    return len(CJK.findall(t or ""))


def _ratio(hit, total):
    return (hit / total) if total else 0.0


# --------------------------------------------------------------------------
# 基础判分器
# --------------------------------------------------------------------------

@checker("exact")
def _exact(resp, spec):
    exp = spec["expect"]
    cands = exp if isinstance(exp, list) else [exp]
    n = norm(resp)
    if spec.get("ci"):
        ok = any(n.casefold() == norm(str(c)).casefold() for c in cands)
    else:
        ok = any(n == norm(str(c)) for c in cands)
    return (1.0 if ok else 0.0), "实际: %r" % n[:80]


@checker("number")
def _number(resp, spec):
    """spec['pick'] 决定取哪个数字，默认 lastline。

    默认取「最后一个含数字的行里的最后一个数」而不是全文首个数字：
    题目要求「写出推理过程，最后给出答案」时，首个数字往往是推理的中间值，
    按首个数字判会把展示过程的模型误判为错——这类误判会静默污染整轮结果。
    """
    body = strip_fence(to_halfwidth(resp)).replace(",", "")
    pick = spec.get("pick", "lastline")
    nums = NUM.findall(body)
    if not nums:
        return 0.0, "未找到数字: %r" % norm(resp)[:60]

    if pick == "first":
        got_s = nums[0]
    elif pick == "last":
        got_s = nums[-1]
    elif pick == "any":
        exp = float(spec["expect"])
        tol = float(spec.get("tol", 1e-9))
        hit = [x for x in nums if abs(float(x) - exp) <= tol]
        return (1.0 if hit else 0.0), "全文数字 %s（期望 %s）" % (nums[:8], spec["expect"])
    else:                                        # lastline
        got_s = nums[-1]
        for line in reversed(body.splitlines()):
            ln = NUM.findall(line)
            if ln:
                got_s = ln[-1]
                break

    tol = float(spec.get("tol", 1e-9))
    ok = abs(float(got_s) - float(spec["expect"])) <= tol
    return (1.0 if ok else 0.0), "取值 %s（期望 %s，pick=%s）" % (got_s, spec["expect"], pick)


_ANS_MARK = re.compile(
    r"(?:答案|答复|结论|选择|应选|选|answer|option|choice)\s*"
    r"(?:是|为|应该?是|应为|选|is|are|should\s+be|:|：|=)?\s*"
    # 字母后必须是边界。缺少 (?![A-Za-z]) 时，"The answer is C" 会把
    # "is" 中的 i 误识别为选项字母。
    r"\(?\s*([A-Ja-j])(?![A-Za-z])\s*[)）.。、]?",
    re.I)
_LEAD = re.compile(r"^\s*[（(\[]?\s*([A-Ja-j])\s*[)）\].、:：]|^\s*([A-Ja-j])\s*$")
_STANDALONE = re.compile(r"(?<![A-Za-z])([A-Ja-j])(?![A-Za-z])")


@checker("mcq")
def _mcq(resp, spec):
    """选择题判分：稳健提取选项字母。

    不用 exact 的原因：标准答案形如「(C)」，而模型可能答「(C) 03/01/2017」
    「C) 06/18/2016」「C」全都被判错——但这些答案在内容上完全正确，错的是
    判分器。公开基准的目的是测知识与推理，不是测它记不记得加括号；把格式
    偏差算成答错，会把能力分数系统性压低。

    提取优先级（先命中先用）：
      1. 显式答案标记，从后往前找（模型常常先推理再给结论）
      2. 整段就是一个字母（可带括号或点号）
      3. 开头的 (X) / X) / X. / X、
      4. 若提供了 options：拿选项原文去比对，模型直接答内容时也能判对
      5. 全文只出现一个合法字母
    """
    exp = re.sub(r"[^A-Za-z]", "", str(spec["expect"])).upper()[:1]
    if not exp:
        return 0.0, "题库里的期望答案不是字母：%r" % spec["expect"]
    opts = spec.get("options") or []
    n = spec.get("n_options") or (len(opts) if opts else 10)
    valid = set("ABCDEFGHIJ"[:max(2, min(n, 10))])

    body = strip_fence(to_halfwidth(resp or "")).strip()
    if not body:
        return 0.0, "空输出"

    def _ok(ch):
        return ch and ch.upper() in valid

    # 1. 显式答案标记，取最后一个
    hits = [m.group(1) for m in _ANS_MARK.finditer(body) if _ok(m.group(1))]
    if hits:
        got, how = hits[-1].upper(), "答案标记"
    else:
        got = how = None
        # 2/3. 整段一个字母，或开头的选项标号
        m = _LEAD.match(body)
        if m:
            ch = m.group(1) or m.group(2)
            if _ok(ch):
                got, how = ch.upper(), "开头选项标号"
        # 4. 拿选项原文比对
        if not got and opts:
            low = body.lower()
            hit = [i for i, o in enumerate(opts)
                   if o and str(o).strip() and str(o).strip().lower() in low]
            if len(hit) == 1 and hit[0] < len(valid):
                got, how = "ABCDEFGHIJ"[hit[0]], "选项原文匹配"
        # 5. 全文唯一合法字母
        if not got:
            uniq = {c.upper() for c in _STANDALONE.findall(body) if _ok(c)}
            if len(uniq) == 1:
                got, how = uniq.pop(), "全文唯一字母"

    if not got:
        return 0.0, "未能识别出选项，原文：%r" % body[:70]
    return (1.0 if got == exp else 0.0), "识别为 %s（%s，期望 %s）" % (got, how, exp)


@checker("span_answer")
def _span_answer(resp, spec):
    """阅读理解的短答案比对（DROP / SQuAD 这类）。

    spec:
      answers      标准答案列表，命中任一即算对
      unanswerable 为 true 表示原文回答不了，正确行为是明说「无法回答」

    不可答题是这类数据集里最有价值的部分——它测的是「原文没有就说没有」，
    对应自建题库 D 组的幻觉主题。
    """
    body = norm(resp)
    low = body.lower()
    NOANS = ["无法回答", "无法确定", "没有提到", "未提及", "文中没有", "不知道",
             "cannot be answered", "can't be answered", "not mentioned",
             "no answer", "unanswerable", "not stated", "does not say",
             "does not mention", "doesn't mention", "not provided",
             "insufficient", "i don't know", "i do not know"]
    said_no = any(k in low for k in NOANS)

    if spec.get("unanswerable"):
        return (1.0 if said_no else 0.0), (
            "正确地拒答" if said_no else "原文无法回答，却给出了答案：%r" % body[:60])

    if said_no:
        return 0.0, "答案在原文中存在，却回答了「无法回答」"

    for a in dict.fromkeys(str(x).strip() for x in (spec.get("answers") or [])):
        if not a:
            continue
        al = a.lower()
        # 短答案（尤其是数字）不能用裸子串匹配：DROP 里大量答案是 "2" 这种，
        # 只要回答里出现过 2（"2 yards"、"12"、"2008"）就会误判为对。
        # 数字用数值比较，短词用词边界，长答案才退回包含匹配。
        if re.fullmatch(r"-?[\d,]+(?:\.\d+)?", a):
            want = float(a.replace(",", ""))
            got = [float(x.replace(",", ""))
                   for x in re.findall(r"-?[\d,]*\d(?:\.\d+)?", body.replace(" ", ""))]
            if any(abs(g - want) < 1e-9 for g in got):
                return 1.0, "数值命中 %s" % a
        elif len(al) <= 12:
            if re.search(r"(?<![0-9A-Za-z])%s(?![0-9A-Za-z])" % re.escape(al), low):
                return 1.0, "命中标准答案 %r（词边界）" % a[:40]
        elif al in low:
            return 1.0, "命中标准答案 %r" % a[:40]
    return 0.0, "未命中任一标准答案 %s；实际 %r" % (
        [str(x)[:20] for x in (spec.get("answers") or [])[:3]], body[:60])


def _words(t):
    """英文词数。中英混排时按空白切，够用。"""
    return len([w for w in re.split(r"\s+", (t or "").strip()) if w])


_IFEVAL = {
    "punctuation:no_comma":
        lambda t, k: "," not in t and "，" not in t,
    "change_case:english_lowercase":
        lambda t, k: t == t.lower(),
    "change_case:english_capital":
        lambda t, k: t == t.upper(),
    "detectable_format:number_bullet_lists":
        lambda t, k: len(re.findall(r"^\s*[-*]\s+", t, re.M)) == k.get("num_bullets"),
    "detectable_format:number_highlighted_sections":
        lambda t, k: len(re.findall(r"\*[^*\n]+\*", t)) >= (k.get("num_highlights") or 1),
    "detectable_content:number_placeholders":
        lambda t, k: len(re.findall(r"\[[^\]\n]+\]", t)) >= (k.get("num_placeholders") or 1),
    "startend:end_checker":
        lambda t, k: t.rstrip().endswith((k.get("end_phrase") or "").strip()),
    "startend:quotation":
        lambda t, k: t.strip().startswith('"') and t.strip().endswith('"'),
    "detectable_format:title":
        lambda t, k: bool(re.search(r"<<[^>]+>>", t)),
}


@checker("ifeval")
def _ifeval(resp, spec):
    """IFEval 的程序化约束校验。

    只实现能可靠机器判定的那几类约束——抓取时也只保留这些题。
    不硬凑覆盖率：判不准的约束宁可不收，否则分数会带上判分器自己的噪声。
    """
    body = strip_fence(resp or "")
    ids = spec.get("instruction_ids") or []
    kws = spec.get("kwargs") or [{}] * len(ids)
    hit, missed = 0, []
    for i, iid in enumerate(ids):
        fn = _IFEVAL.get(iid)
        k = kws[i] if i < len(kws) else {}
        k = {kk: vv for kk, vv in (k or {}).items() if vv is not None}
        if fn is None:
            continue
        try:
            ok = bool(fn(body, k))
        except Exception:
            ok = False
        if iid.startswith("length_constraints:number_words"):
            ok = _check_words(body, k)
        hit += ok
        if not ok:
            missed.append(iid.split(":")[-1])
    total = sum(1 for i in ids if i in _IFEVAL or i.startswith("length_constraints:number_words"))
    if not total:
        return None, "本题的约束类型不支持自动判定"
    return _ratio(hit, total), "满足 %d/%d%s" % (
        hit, total, ("；未满足 " + "、".join(missed)) if missed else "")


def _check_words(t, k):
    n = _words(t)
    rel, want = k.get("relation"), k.get("num_words")
    if want is None:
        return True
    return n >= want if rel == "at least" else (n <= want if rel == "less than" else True)


@checker("contains_all")
def _contains_all(resp, spec):
    exp = spec["expect"]
    hit = [x for x in exp if x in resp]
    miss = [x for x in exp if x not in resp]
    return _ratio(len(hit), len(exp)), ("缺: " + ", ".join(miss)) if miss else "全部命中"


@checker("contains_any")
def _contains_any(resp, spec):
    hit = [x for x in spec["expect"] if x in resp]
    return (1.0 if hit else 0.0), ("命中: " + hit[0]) if hit else "未命中任一预期表述"


@checker("contains_none")
def _contains_none(resp, spec):
    bad = [x for x in spec["expect"] if x in resp]
    return (0.0 if bad else 1.0), ("出现禁用: " + ", ".join(bad)) if bad else "无禁用内容"


#: 短模式只在回复尾部这么多字符里搜。
#:
#: 为什么要限：`re.search` 搜整段回复时，`\b5\b` 这种短模式会被思维链里
#: 任何一处「5」命中 —— 模型最终答错、但推理过程中提到过正确数字，照样满分。
#: 判分器只会把分往高判，而虚高的分比判错更危险：它看不出来。
#:
#: 阈值取自 1.3 实测：54 条短模式响应上，搜全文与搜结尾 200 字符**结果完全一致**
#: （54 : 54），所以这个窗口是零回归的。更紧的「只搜最后一行」会误伤 4 条 ——
#: 有的回复末行是 `\]`，有的答案在倒数第二行。
#:
#: ⚠ 这个窗口**缩小**误命中面，但不能根除：如果推理的最后一段就紧贴答案，
#: 窗口里照样会夹进推理文本。要根除只有两条路 ——
#: 题面强制模型输出定界的答案，或者做一个专门的答案抽取判据。
#: 都属于后续维护，不在本版范围。
REGEX_TAIL_WINDOW = 200

#: 期望串短于这个长度就按「短模式」处理。长模式（几十上百字符）本身
#: 就足够特异，误命中的概率可以忽略，而且它们可能合理地出现在正文中段。
REGEX_SHORT = 12


@checker("regex")
def _regex(resp, spec):
    flags = re.S | (re.I if spec.get("ci") else 0)
    pat = spec["expect"]
    body = resp or ""
    scope, note = body, ""
    if len(pat) <= REGEX_SHORT and len(body) > REGEX_TAIL_WINDOW:
        scope = body.rstrip()[-REGEX_TAIL_WINDOW:]
        note = "（短模式，只搜结尾 %d 字符）" % REGEX_TAIL_WINDOW
    ok = re.search(pat, scope, flags) is not None
    return (1.0 if ok else 0.0), ("匹配" if ok else "未匹配") + note


@checker("cn_count")
def _cn_count(resp, spec):
    c = cn_count(strip_fence(resp))
    lo = spec.get("min", spec.get("expect"))
    hi = spec.get("max", spec.get("expect"))
    return (1.0 if lo <= c <= hi else 0.0), "汉字数=%d（要求 %s~%s）" % (c, lo, hi)


@checker("length")
def _length(resp, spec):
    """仅记录，不判分——用于 P4 过度思考这类观测题。"""
    return None, "输出 %d 字符 / %d 汉字" % (len(resp), cn_count(resp))


# --------------------------------------------------------------------------
# 得分点型
# --------------------------------------------------------------------------

@checker("checkpoints")
def _checkpoints(resp, spec):
    """spec['points'] = [{"label":..., "any":[...]} 或 {"label":..., "all":[...]},
                         可选 "none":[...] 表示不得出现]"""
    pts = spec["points"]
    hit, missed = 0, []
    for p in pts:
        ok = True
        if "any" in p:
            ok = ok and any(x in resp for x in p["any"])
        if "all" in p:
            ok = ok and all(x in resp for x in p["all"])
        if "none" in p:
            ok = ok and not any(x in resp for x in p["none"])
        if "regex" in p:
            ok = ok and re.search(p["regex"], resp, re.S) is not None
        if ok:
            hit += 1
        else:
            missed.append(p.get("label", "?"))
    detail = "命中 %d/%d" % (hit, len(pts))
    if missed:
        detail += "；未命中: " + "、".join(missed[:6])
    return _ratio(hit, len(pts)), detail


# --------------------------------------------------------------------------
# 结构化
# --------------------------------------------------------------------------

@checker("json_strict")
def _json_strict(resp, spec):
    checks, passed = [], 0
    no_fence = "```" not in resp
    checks.append(("无围栏", no_fence))
    try:
        obj = json.loads(strip_fence(resp).strip())
        checks.append(("可解析", True))
    except Exception as e:
        checks.append(("可解析", False))
        return _ratio(sum(1 for _, o in checks if o), len(checks) + 1), "JSON 解析失败: %s" % e
    for k in spec.get("required", []):
        checks.append(("有字段 %s" % k, isinstance(obj, dict) and k in obj))
    for k in spec.get("null_fields", []):
        checks.append(("%s 为 null" % k, isinstance(obj, dict) and obj.get(k, "X") is None))
    for k, v in (spec.get("equals") or {}).items():
        checks.append(("%s == %r" % (k, v), isinstance(obj, dict) and obj.get(k) == v))
    for k, allowed in (spec.get("enum") or {}).items():
        checks.append(("%s 在枚举内" % k, isinstance(obj, dict) and obj.get(k) in allowed))
    passed = sum(1 for _, o in checks if o)
    bad = [n for n, o in checks if not o]
    return _ratio(passed, len(checks)), ("未通过: " + "、".join(bad)) if bad else "全部通过"


def _flatten_tree(node, parent_id, acc):
    acc.append((node.get("id"), node.get("name"), parent_id))
    for ch in node.get("children") or []:
        _flatten_tree(ch, node.get("id"), acc)


@checker("tree")
def _tree(resp, spec):
    """J1 双表示一致性：flat / nested 必须等价，且引用完整。"""
    checks = [("无围栏", "```" not in resp)]
    try:
        obj = json.loads(strip_fence(resp).strip())
        checks.append(("可解析", True))
    except Exception as e:
        checks.append(("可解析", False))
        return _ratio(1 if checks[0][1] else 0, 7), "JSON 解析失败: %s" % e

    flat, nested = obj.get("flat"), obj.get("nested")
    ok_shape = isinstance(flat, list) and isinstance(nested, dict)
    checks.append(("含 flat/nested", ok_shape))
    if not ok_shape:
        return _ratio(sum(1 for _, o in checks if o), 7), "缺少 flat 或 nested"

    ids = [n.get("id") for n in flat]
    idset = set(ids)
    checks.append(("id 无重复", len(ids) == len(idset)))
    checks.append(("id 连续", sorted(x for x in ids if isinstance(x, int)) ==
                   list(range(1, len(ids) + 1))))
    if "n_nodes" in spec:
        checks.append(("节点数正确", len(flat) == spec["n_nodes"]))
    checks.append(("引用完整", all(n.get("parent_id") is None or n.get("parent_id") in idset
                                   for n in flat)))
    checks.append(("单根", sum(1 for n in flat if n.get("parent_id") is None) == 1))
    acc = []
    _flatten_tree(nested, None, acc)
    a = {(n.get("id"), n.get("name"), n.get("parent_id")) for n in flat}
    checks.append(("flat≡nested", a == set(acc)))

    passed = sum(1 for _, o in checks if o)
    bad = [n for n, o in checks if not o]
    return _ratio(passed, len(checks)), ("未通过: " + "、".join(bad)) if bad else "全部通过"


@checker("bulk")
def _bulk(resp, spec):
    """J2 规模化一致性。得分 = 合规行数 / 期望行数，另报首个出错行。"""
    n_expect = spec.get("n_lines", 60)
    id_start = spec.get("id_start", 1001)
    fields = set(spec.get("fields", ["id", "name", "score", "status"]))
    special = {int(k): v for k, v in (spec.get("special_status") or {}).items()}
    default = spec.get("default_status", "ok")

    lines = [l for l in strip_fence(resp).strip().splitlines() if l.strip()]
    good, first_err = 0, None
    for i, line in enumerate(lines, 1):
        try:
            o = json.loads(line)
        except Exception:
            first_err = first_err or i
            continue
        ok = (set(o.keys()) == fields
              and o.get("id") == id_start + i - 1
              and o.get("status") == special.get(i, default))
        if ok:
            good += 1
        else:
            first_err = first_err or i
    detail = "合规 %d/%d 行" % (good, n_expect)
    if first_err:
        detail += "；首个出错行 %d（保真长度）" % first_err
    return _ratio(good, n_expect), detail


# --------------------------------------------------------------------------
# 专项
# --------------------------------------------------------------------------

@checker("self_ref")
def _self_ref(resp, spec):
    """A1 自指不动点：文中声称的三个数字必须与实际相符。"""
    body = strip_fence(resp).strip()
    actual = {
        "汉字数": cn_count(body),
        "「一」次数": body.count("一"),
        "逗号数": body.count("，") + body.count(","),
    }
    nums = set()
    for m in re.finditer(r"\d+|[零〇一二两三四五六七八九十百]+", body):
        tok = m.group()
        v = int(tok) if tok.isdigit() else _cn_num(tok)
        if v is not None:
            nums.add(v)
    hit = sum(1 for v in actual.values() if v in nums)
    return _ratio(hit, 3), "实际 %s；文中数字 %s" % (actual, sorted(nums)[:12])


_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num(s):
    if s in _CN_DIGIT:
        return _CN_DIGIT[s]
    total = section = 0
    seen = False
    for ch in s:
        if ch in _CN_DIGIT:
            section = _CN_DIGIT[ch]
            seen = True
        elif ch == "十":
            section = (section or 1) * 10
            total += section
            section, seen = 0, True
        elif ch == "百":
            section = (section or 1) * 100
            total += section
            section, seen = 0, True
        else:
            return None
    return total + section if seen else None


@checker("lines_spec")
def _lines_spec(resp, spec):
    """A2 耦合约束：逐条校验行数、字数、首字、禁用词、标点。"""
    lines = [re.sub(r"^\s*\d+[.、)]\s*", "", l.strip())
             for l in strip_fence(resp).strip().splitlines() if l.strip()]
    checks = []
    n_want = spec.get("n_lines")
    checks.append(("行数", len(lines) == n_want))
    if len(lines) == n_want:
        if "cn_lens" in spec:
            got = [cn_count(l) for l in lines]
            checks.append(("字数 %s（实际 %s）" % (spec["cn_lens"], got),
                           got == spec["cn_lens"]))
        if "first_chars" in spec:
            got = "".join(l[0] for l in lines)
            checks.append(("首字 %s（实际 %s）" % (spec["first_chars"], got),
                           got == spec["first_chars"]))
        if "line_regex" in spec:
            for idx, pat in spec["line_regex"].items():
                i = int(idx) - 1
                checks.append(("第%s句匹配" % idx,
                               bool(re.search(pat, lines[i])) if 0 <= i < len(lines) else False))
        if "end_punct" in spec:
            ok = all(lines[int(k) - 1].endswith(v) for k, v in spec["end_punct"].items()
                     if 0 < int(k) <= len(lines))
            checks.append(("句末标点", ok))
    body = "".join(lines)
    if "banned" in spec:
        bad = [w for w in spec["banned"] if w in body]
        checks.append(("禁用词（出现 %s）" % bad if bad else "禁用词", not bad))
    passed = sum(1 for _, o in checks if o)
    bad = [n for n, o in checks if not o]
    return _ratio(passed, len(checks)), "满足 %d/%d；未过: %s" % (
        passed, len(checks), "、".join(bad) if bad else "无")


@checker("banned_and_length")
def _banned_and_length(resp, spec):
    """A4 负约束：禁用词 + 篇幅。"""
    body = strip_fence(resp).strip()
    hits = [w for w in spec["banned"] if w in body]
    c = cn_count(body)
    lo, hi = spec.get("min", 0), spec.get("max", 10 ** 9)
    checks = [("禁用词", not hits), ("篇幅", lo <= c <= hi)]
    passed = sum(1 for _, o in checks if o)
    return _ratio(passed, 2), "禁用词命中 %s；汉字 %d（要求 %d~%d）" % (hits or "无", c, lo, hi)


@checker("chain_compound")
def _chain_compound(resp, spec):
    """P1 链-答一致性：逐年自洽 + 结论与过程一致 + 终值正确。"""
    p0 = float(spec.get("principal", 87650))
    rate = float(spec.get("rate", 1.0435))
    years = int(spec.get("years", 7))
    final = float(spec["final"])

    nums = [float(m.group().replace(",", ""))
            for m in re.finditer(r"\d[\d,]{3,}\.\d{2}", resp)]
    series = [x for x in nums if x >= p0 * 0.5]
    if not series:
        return 0.0, "未找到金额序列"
    if len(series) >= years + 1:
        chain, concl = series[:years], series[-1]
    else:
        chain, concl = series, series[-1]

    prev, bad = p0, 0
    for v in chain:
        if abs(v / prev - rate) > 0.0005:
            bad += 1
        prev = v
    checks = [
        ("逐年数量", len(chain) == years),
        ("逐年自洽", bad == 0),
        ("链答一致", bool(chain) and abs(concl - chain[-1]) < 0.005),
        ("终值正确", abs(concl - final) < 0.005),
    ]
    passed = sum(1 for _, o in checks if o)
    bad_names = [n for n, o in checks if not o]
    return _ratio(passed, 4), "结论 %.2f；过程末值 %s；未过: %s" % (
        concl, ("%.2f" % chain[-1]) if chain else "-",
        "、".join(bad_names) if bad_names else "无")


_LATEX_DROP = [r"\left", r"\right", r"\displaystyle", r"\!", r"\,", r"\;", r"\ ",
               "$", " ", "\n", "\t"]


#: 行内／行间公式的定界符。它们不是答案的一部分，比对前必须剥掉。
_MATH_DELIMS = (r"\[", r"\]", r"\(", r"\)", "$$", "$")

#: 答案前缀。模型常写 "answer: 42" / "答案：42"，冒号后才是答案。
_ANSWER_PREFIX = re.compile(
    r"^\s*(?:final\s+)?(?:answer|ans|result|答案|结果)\s*(?:is)?\s*[:：]?\s*",
    re.I)


def _strip_math_delims(s):
    """剥掉包裹整个答案的公式定界符。

    只剥**首尾**的定界符，不做全局替换 —— `$` 在正文里可能是货币符号。
    """
    s = s.strip()
    changed = True
    while changed:
        changed = False
        for d in _MATH_DELIMS:
            if s.startswith(d):
                s = s[len(d):].strip()
                changed = True
            if s.endswith(d):
                s = s[:-len(d)].strip()
                changed = True
    return s


def _last_boxed(s):
    """取最后一个 `\\boxed{...}` 的内容，**大括号配平**。返回 None 表示没有。

    不能用正则：`\\boxed{\\frac{2619}{10}}` 里
      · 非贪婪 `\\boxed\\{(.+?)\\}` 停在 `\\frac{2619}` 的内层 `}`，得到 `\\frac{2619`
      · 贪婪 `(.+)\\}` 又会吞掉后面别的东西
    LaTeX 是嵌套结构，只能扫括号。
    """
    key = r"\boxed{"
    start = s.rfind(key)
    if start < 0:
        return None
    i = start + len(key)
    depth = 1
    while i < len(s):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[start + len(key):i]
        i += 1
    return None                    # 括号没闭合（多半被截断了）


def _math_norm(s):
    """轻量 LaTeX 归一化。只做无争议的等价变换，不做符号计算。"""
    s = str(s).strip()
    s = _strip_math_delims(s)
    s = _ANSWER_PREFIX.sub("", s)
    # 取**最后一个** \boxed{} 的内容。
    #
    # 旧写法是 r"\\boxed\{(.+)\}\s*$"，锚在字符串末尾。实测踩到：模型输出
    # `\[\n\boxed{1}\n\]`，`\boxed{}` 后面还有一个 `\]`（不是空白），
    # 锚点失配 → 提取彻底失效 → 答对了却判 0 分。
    # 而 `\[ ... \boxed{} ... \]` 正是推理模型呈现数学答案的标准格式，
    # 所以这一条几乎命中全部 math 题：实施计划第 1.3 项 实测 16 条未截断的
    # LiveBench 数学题**全部 0 分**，而回复里的答案是对的。
    boxed = _last_boxed(s)
    if boxed is not None:
        s = _strip_math_delims(boxed)
    s = s.replace(r"\dfrac", r"\frac").replace(r"\tfrac", r"\frac")
    s = s.replace(r"\cdot", "*").replace(r"\times", "*")
    for t in _LATEX_DROP:
        s = s.replace(t, "")
    s = s.rstrip(".。")
    s = re.sub(r"^\\text\{(.*)\}$", r"\1", s)
    return s.lower()


def _as_float(s):
    s = str(s).replace(",", "").strip()
    m = re.fullmatch(r"-?\d+(?:\.\d+)?", s)
    if m:
        return float(s)
    m = re.fullmatch(r"\\?frac\{(-?\d+)\}\{(-?\d+)\}", s)      # \frac{a}{b}
    if m and float(m.group(2)) != 0:
        return float(m.group(1)) / float(m.group(2))
    m = re.fullmatch(r"(-?\d+)/(-?\d+)", s)
    if m and float(m.group(2)) != 0:
        return float(m.group(1)) / float(m.group(2))
    return None


@checker("math_equal")
def _math_equal(resp, spec):
    """数学答案比对：归一化字符串相等，或数值相等。

    只做保守的等价判断——形式不同但数学等价的答案（如 0.5 与 1/2 之外的
    复杂情形）会被判错。这是刻意的：宁可低估，不可虚高。
    """
    exp = str(spec["expect"])
    body = strip_fence(resp).strip()
    # 找最后一个**有内容**的行。只由公式定界符组成的行（`\]`、`$$`）
    # 要跳过——它们是排版，不是答案。
    last = ""
    for line in reversed(body.splitlines()):
        t = line.strip()
        if t and _strip_math_delims(t):
            last = t
            break
    cands = [body, last]

    ne = _math_norm(exp)
    fe = _as_float(ne)
    for c in cands:
        nc = _math_norm(c)
        if nc == ne:
            return 1.0, "字符串等价（%s）" % nc[:40]
        fc = _as_float(nc)
        if fe is not None and fc is not None and abs(fc - fe) < 1e-6:
            return 1.0, "数值等价（%s ≈ %s）" % (fc, fe)
    return 0.0, "期望 %r，实际末行 %r" % (ne[:40], _math_norm(last)[:40])


@checker("exec_python")
def _exec_python(resp, spec):
    """执行模型生成的代码跑单元测试。

    ⚠ 这会在本机运行模型输出的任意 Python 代码。默认禁用，必须显式设置
    环境变量 MPROBE_ALLOW_EXEC=1 才会真正执行；否则返回「未启用」。
    子进程隔离 + 超时，但**不是沙箱**——只在你信任被测模型时开启。
    """
    if os.environ.get("MPROBE_ALLOW_EXEC") != "1":
        return None, "代码执行未启用（需设 MPROBE_ALLOW_EXEC=1）"

    import subprocess
    import tempfile

    code = strip_fence(resp)
    if "```" in code:                                   # 取第一个 python 代码块
        m = re.search(r"```(?:python)?\s*\n(.*?)```", resp, re.S)
        if m:
            code = m.group(1)
    entry = spec.get("entry_point") or "candidate"
    prog = "\n".join([spec.get("preamble") or "", code, spec.get("test") or "",
                      "check(%s)" % entry, "print('__PASS__')"])

    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as f:
            f.write(prog)
            path = f.name
        r = subprocess.run([sys.executable, path], capture_output=True,
                           text=True, timeout=spec.get("timeout", 15))
        if "__PASS__" in (r.stdout or ""):
            return 1.0, "全部测试通过"
        err = (r.stderr or "").strip().splitlines()
        return 0.0, "测试失败：%s" % (err[-1][:90] if err else "无输出")
    except subprocess.TimeoutExpired:
        return 0.0, "执行超时（可能死循环）"
    except Exception as e:
        return 0.0, "执行异常：%s" % str(e)[:80]
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


@checker("calibration")
def _calibration(resp, spec):
    """K1 单题：答案正确性 + 置信度。分数只记正确性，置信度另行汇总。"""
    m = re.search(r"置信度\s*[:：]\s*(\d{1,3})", resp)
    conf = int(m.group(1)) if m else None
    sub = spec.get("answer_check")
    if sub:
        score, detail = run_check(resp, sub)
    else:
        score, detail = None, "无答案判据"
    return score, "置信度=%s；%s" % (conf if conf is not None else "未给出", detail)


def extract_confidence(resp):
    m = re.search(r"置信度\s*[:：]\s*(\d{1,3})", resp or "")
    return int(m.group(1)) if m else None
