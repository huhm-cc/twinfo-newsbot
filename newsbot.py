#!/usr/bin/env python3
"""Taiwan INFO newsbot：抓台灣新聞 RSS → 去重、合併同一事件 → 經網站 /api/news/rewrite 兩階段 AI：
AI#1 撰稿（role=writer，NVIDIA nemotron-3.5-bolt-e4b）→ 程式檢查（抄寫、字數、引號名稱、台灣視角代稱）→
AI#2 審稿（role=reviewer，NVIDIA nemotron-3-ultra-550b-a55b，關思考，附原始報導全文）→ 有錯退回 AI#1，最多 3 輪 → 通過才 POST 到 twinfo.huhm.cc。

只用 Python 標準函式庫。環境變數：
  NEWS_INGEST_TOKEN       （可選）網站寫入 token；沒有時改用 GitHub Actions OIDC（permissions: id-token: write）
  SITE                    預設 https://twinfo.huhm.cc
  MAX_ARTICLES            每次最多改寫幾則（預設 2，新事件很多時 3）
  REVIEW_MODELS           審稿模型（預設 nvidia/nemotron-3-ultra-550b-a55b）
  TIME_BUDGET             秒，超過不再開始新的一篇（預設 390）
  MODELS                  逗號分隔，依序嘗試（遇到額度用完 429 換下一個，全部用完就跳過，不算失敗）
  DAILY_CAP               每天最多幾篇（預設 80，依時間平均分配）
  DRY_RUN=1               不呼叫模型也不寫入，只印出選到的事件
"""
import concurrent.futures as cf
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
SITE = os.environ.get("SITE", "https://twinfo.huhm.cc").rstrip("/")
UA = "Mozilla/5.0 (compatible; twinfo-newsbot/1.0; +https://github.com/huhm-cc/twinfo-newsbot)"
MODELS = [m.strip() for m in os.environ.get("MODELS", "nvidia/nemotron-3.5-bolt-e4b,google/gemini-2.5-flash-lite").split(",") if m.strip()]
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "2"))
DRY = os.environ.get("DRY_RUN") == "1"
DAILY_CAP = int(os.environ.get("DAILY_CAP", "80"))  # 每天最多幾篇，平均分散到 24 小時（控制 LLM 免費額度）
MAX_AGE_H = 8  # 只處理 8 小時內的新聞
REVIEW_MAX_TOKENS = int(os.environ.get("REVIEW_MAX_TOKENS", "3000"))
TIME_BUDGET = int(os.environ.get("TIME_BUDGET", "390"))  # 秒；超過就不再開始新的一篇（workflow timeout 9 分鐘）

# (來源名稱, RSS 網址, 預設分類, 權重)
FEEDS = [
    ("中央社", "https://feeds.feedburner.com/rsscna/politics", "news", 3),
    ("中央社", "https://feeds.feedburner.com/rsscna/finance", "news", 3),
    ("中央社", "https://feeds.feedburner.com/rsscna/technology", "tech", 3),
    ("中央社", "https://feeds.feedburner.com/rsscna/social", "news", 2),
    ("中央社", "https://feeds.feedburner.com/rsscna/lifehealth", "news", 2),
    ("中央社", "https://feeds.feedburner.com/rsscna/mainland", "news", 2),
    ("公視新聞", "https://news.pts.org.tw/xml/newsfeed.xml", "news", 3),
    ("自由時報", "https://news.ltn.com.tw/rss/politics.xml", "news", 2),
    ("自由時報", "https://news.ltn.com.tw/rss/business.xml", "news", 2),
    ("自由時報", "https://news.ltn.com.tw/rss/life.xml", "news", 1),
    ("科技新報", "https://technews.tw/feed/", "tech", 2),
    ("iThome", "https://www.ithome.com.tw/rss", "tech", 2),
    ("INSIDE", "https://www.inside.com.tw/feed/rss", "tech", 1),
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http(url, data=None, headers=None, timeout=25, method=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "ignore")


# ---------------- RSS ----------------
def _tag(block, *names):
    for n in names:
        m = re.search(rf"<{n}(?:\s[^>]*)?>(.*?)</{n}>", block, re.S)
        if m:
            v = m.group(1).strip()
            v = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", v, flags=re.S)
            return html.unescape(v).strip()
        m = re.search(rf"<{n}\s[^>]*href=\"([^\"]+)\"", block)
        if m:
            return html.unescape(m.group(1))
    return ""


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except Exception:
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00").replace("  ", " "))
        except Exception:
            m = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})", s)
            if not m:
                return None
            d = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}")
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)  # 沒寫時區的（如 iThome）視為台北時間
    return d.astimezone(TZ)


def clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_feed(f):
    name, url, cat, w = f
    try:
        _, x = http(url, timeout=20)
    except Exception as e:
        log(f"[feed] {name} {url} 失敗：{e}")
        return []
    out = []
    for b in re.findall(r"<item[\s>].*?</item>|<entry[\s>].*?</entry>", x, re.S):
        title = clean_text(_tag(b, "title"))
        link = _tag(b, "link", "guid").strip()
        if not title or not link.startswith("http"):
            continue
        pub = parse_date(_tag(b, "pubDate", "published", "updated", "dc:date"))
        desc = clean_text(_tag(b, "description", "summary", "content:encoded"))[:600]
        out.append({"source": name, "url": canon(link), "title": title, "published": pub, "desc": desc, "cat": cat, "w": w})
    return out


def canon(u):
    p = urllib.parse.urlsplit(u.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith(("utm_", "from", "ref", "fbclid"))]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


# ---------------- 相似度 ----------------
PUNCT = re.compile(r"[\s\W_]+", re.U)
STOP = set("的了是在與和及為將對於從被也")


def norm(t):
    t = re.sub(r"[〔【\[（(].{0,12}?[〕】\]）)]", "", t)  # 去掉〔記者…〕、【影】之類
    return PUNCT.sub("", t.lower())


def grams(t):
    t = norm(t)
    return {t[i:i + 2] for i in range(len(t) - 1)} - {a + b for a in STOP for b in STOP}


def sim(a, b):
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))  # overlap coefficient


SAME_EVENT = 0.5


# ---------------- 原文擷取 ----------------
def extract(url, page):
    page = re.sub(r"<(script|style|noscript|iframe|svg|figure|figcaption|aside|nav|footer|header)[\s>].*?</\1>", " ", page, flags=re.S | re.I)
    body = page
    for pat in [r'<div[^>]+class="[^"]*paragraph[^"]*"', r'<div[^>]+class="[^"]*\btext\b[^"]*boxText', r'<div[^>]+class="[^"]*post-content', r'<div[^>]+class="[^"]*entry-content',
                r'<div[^>]+class="[^"]*field-name-body', r'<div[^>]+class="[^"]*article-content', r'<div[^>]+class="[^"]*articleBody', r"<article[\s>]"]:
        m = re.search(pat, page, re.I)
        if m:
            body = page[m.start():m.start() + 60000]
            break
    paras = []
    for m in re.findall(r"<p[^>]*>((?:(?!<p[\s>]).)*?)</p>", body, re.S | re.I):
        t = clean_text(m)
        if len(t) < 25 or len(t) > 1500:
            continue
        if re.search(r"(請繼續往下閱讀|不用抽|點我訂閱|加入會員|下載APP|看更多|延伸閱讀|版權所有|著作權|未經授權|本網站使用|隱私|cookie|訂閱電子報|追蹤我們|按讚|分享至|選擇與事實站在一起|非經授權|其他裝置進行登入|擔心錯過|繼續閱讀|免費電子報|贊助)", t, re.I):
            continue
        if t not in paras:
            paras.append(t)
    return "\n".join(paras)


def fetch_body(it):
    try:
        _, p = http(it["url"], timeout=20)
        t = extract(it["url"], p)
    except Exception as e:
        log(f"[page] {it['url']} 失敗：{e}")
        t = ""
    if len(t) < 120:
        t = (it.get("desc") or "") + ("\n" + t if t else "")
    return t.strip()


# ---------------- 網站 ----------------
_auth = None


def site_auth():
    global _auth
    if _auth:
        return _auth
    tok = os.environ.get("NEWS_INGEST_TOKEN", "").strip()
    if not tok:
        u, t = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"), os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        if not u:
            raise RuntimeError("沒有 NEWS_INGEST_TOKEN，也不在有 id-token 權限的 GitHub Actions 裡")
        _, b = http(u + "&audience=twinfo.huhm.cc", headers={"Authorization": f"bearer {t}"})
        tok = json.loads(b)["value"]
    _auth = {"Authorization": f"Bearer {tok}"}
    return _auth


def site_recent():
    _, b = http(f"{SITE}/api/news/ingest/recent", headers=site_auth())
    return json.loads(b)["items"]


def site_post(items):
    data = json.dumps({"items": items}, ensure_ascii=False).encode()
    try:
        _, b = http(f"{SITE}/api/news/ingest", data=data, headers={**site_auth(), "content-type": "application/json"}, method="POST", timeout=40)
        return json.loads(b)
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode("utf-8", "ignore")[:500]}


# ---------------- 模型 ----------------
class QuotaExhausted(Exception):
    pass


dead_models = set()


# 審稿模型：2026-11-22 實測 nemotron-3.5-lightning-30b-a3b 不穩定（開思考會把 6000 token 用完、漏抓安聯投信誤譯；關思考會要求標題照抄原文、輸出到上限），
# 改用同一把 NVIDIA 金鑰的 nemotron-3-ultra-550b-a55b（關思考，約 5–20 秒，能抓到 Union Asset Management、earnings per share 誤譯）
REVIEW_MODELS = [m.strip() for m in os.environ.get("REVIEW_MODELS", "nvidia/nemotron-3-ultra-550b-a55b").split(",") if m.strip()]
REVIEW_THINKING = os.environ.get("REVIEW_THINKING", "0") == "1"


def chat(messages, max_tokens=2600, temperature=0.4, role="writer", thinking=None):
    """經由網站 /api/news/rewrite 呼叫 LLM（GitHub Models 已於 2026-07-30 停止服務）。
    網站端主要用 NVIDIA API（NVIDIA_API_KEY），備援 Google AI Studio／Vercel AI Gateway；額度用完／尚未開通時換下一個模型，全部不行就跳過本次。"""
    for m in (REVIEW_MODELS if role == "reviewer" else MODELS):
        if m in dead_models:
            continue
        req = {"role": role, "model": m, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if thinking is not None:
            req["thinking"] = thinking
        body = json.dumps(req).encode()
        for attempt in range(2):
            try:
                st, b = http(f"{SITE}/api/news/rewrite", data=body, method="POST", timeout=150, headers={**site_auth(), "content-type": "application/json"})
                j = json.loads(b)
                return m, j["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "ignore")[:300]
                log(f"[model] {m} HTTP {e.code}: {msg}")
                if e.code == 429 and attempt == 0:
                    ra = int(e.headers.get("retry-after", "0") or 0)
                    if 0 < ra <= 65:
                        time.sleep(ra)
                        continue
                if e.code >= 500 and attempt == 0:
                    time.sleep(5)
                    continue
                dead_models.add(m)  # 額度用完、未開通、模型不存在
                break
            except Exception as e:
                log(f"[model] {m} 錯誤：{e}")
                if attempt == 0:
                    time.sleep(3)
                    continue
                dead_models.add(m)
    raise QuotaExhausted(f"所有{'審稿' if role == 'reviewer' else '撰稿'}模型都無法使用（LLM 額度用完或尚未開通）")


SYSTEM = """你是 Taiwan INFO 台灣資訊的新聞編輯。你會拿到同一則新聞事件的一或多篇原始報導，請「用自己的話」重新寫成一篇新的新聞稿，避免任何著作權爭議。

改寫規則（一定要遵守）：
1. 不可逐句翻抄或只換幾個字；要重新組織結構與句子，用你自己的措辭。不可大段引用原文；直接引語最多一句、且不超過 20 字並註明是誰說的，其餘一律改為轉述。
2. 只能使用原始報導裡有的事實。人名、職稱、機構名、地名、數字、金額、百分比、日期照原文保留，不可更改、不可推測、不可補充原文沒有的內容。
3. 中立客觀：不加評論、不下結論、不用情緒或煽動字眼、不臆測；有爭議時並陳各方說法。不寫「本報」「記者」「綜合報導」等字樣。
4. 中文用繁體中文與台灣用語、全形標點。日期寫成完整日期（例如 2026 年 11 月 22 日），不要只寫「今天」「22 日」。
5. 中文正文 3–5 段，段落之間用 \\n\\n 分隔，總長 260–450 字（不含標點），第一段是導言（誰、什麼事、何時、何地）。
6. 英文正文是同一篇的英文版（不是逐字翻譯也可），3–5 段，180–320 個英文字，使用一般國際媒體寫法（例如 Taiwan's Central Election Commission、New Taiwan dollars (NT$)）。
7. 不要在文末列出來源、不要 hashtag、不要 emoji、不要問句互動。
8. 英文版金額單位換算要正確：1 億 = 100 million，10 億 = 1 billion。例如 811 億元 = NT$81.1 billion，811.8 億元 = NT$81.18 billion（保留原數字的精確度），98.5 億元 = NT$9.85 billion，3.5 億元 = NT$350 million，1.2 兆元 = NT$1.2 trillion；美元同理（US$）。本益比是 price-to-earnings (P/E) ratio。
9. 「」引號裡的活動名稱、口號、政策名稱、作品名稱（例如「嘉鄉味」）中文版一字不改照抄，不可換成同音字。英文專有名詞不可自創：民進黨 = Democratic Progressive Party (DPP)，國民黨 = Kuomintang (KMT)，民眾黨 = Taiwan People's Party (TPP)，時代力量 = New Power Party，金管會 = Financial Supervisory Commission，中選會 = Central Election Commission，九合一選舉 = local elections，立法院 = Legislative Yuan，行政院 = Executive Yuan。公司或機構若不確定官方英文名，用通行英文名（例如 安聯投信 = Allianz Global Investors Taiwan），仍不確定就用羅馬拼音，不可翻成意思不同的名稱。

10. 不要用站在台灣視角的代稱「本國」「我國」「國內」「本土」「國人」「本國銀行」等，一律明寫「台灣」（例如 本國銀行 → 台灣的銀行、國內市場 → 台灣市場、國人 → 台灣民眾）；英文用 Taiwan／Taiwanese（例如 Taiwan's banks、Taiwanese banks），不可用 domestic、our country、local banks。

只輸出一個 JSON 物件（不要 ``` 程式碼區塊、不要任何說明文字），欄位：
{"category": "news" 或 "tech"（科技、AI、半導體、資安、網路、電子產業歸 tech，其餘 news）,
 "title_zh": "中文標題，14–30 字，不可與任何原始標題相同",
 "title_en": "English headline",
 "summary_zh": "一句話中文摘要，40–80 字",
 "summary_en": "One-sentence English summary",
 "article_zh": "中文正文",
 "article_en": "English article",
 "tags": ["3–6 個中文關鍵字"],
 "slug": "english-url-slug-3-to-8-words"}"""


GLOSSARY = [("民進黨", "Democratic Progressive Party"), ("國民黨", "Kuomintang"), ("民眾黨", "Taiwan People's Party"), ("金管會", "Financial Supervisory Commission"), ("中選會", "Central Election Commission"), ("立法院", "Legislative Yuan"), ("行政院", "Executive Yuan")]


def han_len(s):
    return len(re.sub(r"[\s\W_]", "", s or ""))


def copied_segments(art, src, n=18):
    """回傳文章裡與原文連續相同 ≥ n 個字（去標點空白後）的片段。"""
    a, s = norm(art), norm(src)
    out, i = [], 0
    while i + n <= len(a):
        if a[i:i + n] in s:
            j = i + n
            while j < len(a) and a[i:j + 1] in s:
                j += 1
            out.append(a[i:j])
            i = j
        else:
            i += 1
    return out


def shingle_ratio(art, src, k=8):
    a, s = norm(art), norm(src)
    sh = {a[i:i + k] for i in range(len(a) - k + 1)}
    if not sh:
        return 0.0
    return sum(1 for x in sh if x in s) / len(sh)


def parse_json_obj(out):
    """模型可能把 JSON 包在說明文字或 ``` 裡，或先輸出 <think>…</think>；找出第一個可解析的 JSON 物件。"""
    s = re.sub(r"<think>.*?</think>", "", str(out or ""), flags=re.S).strip()
    cands = [s] + [m.strip() for m in re.findall(r"```(?:json|JSON)?\s*(.*?)```", s, re.S)]
    for c in cands:
        try:
            v = json.loads(c, strict=False)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
    dec = json.JSONDecoder(strict=False)  # 容忍字串裡的原始換行
    i = s.find("{")
    while i >= 0:
        try:
            v, _ = dec.raw_decode(s[i:])
            if isinstance(v, dict):
                return v
        except Exception:
            pass
        i = s.find("{", i + 1)
    raise ValueError("JSON 解析失敗")


TW_POV_ZH = re.compile(r"(本國|我國|國內(?!生產毛額)|(?<!台灣)本土|國人)")
TW_POV_EN = re.compile(r"\b(domestic|our country|local banks?)\b", re.I)


def source_text(cluster, full=False):
    """撰稿用每篇截斷；審稿用全文（full=True，總長上限約 16000 字）。"""
    blocks = []
    per = max(1200, 4800 // len(cluster)) if not full else max(4000, 16000 // len(cluster))
    for i, it in enumerate(cluster, 1):
        pub = it["published"].strftime("%Y-%m-%d %H:%M") if it.get("published") else "不明"
        blocks.append(f"【報導 {i}】來源：{it['source']}｜發布時間（台北）：{pub}\n標題：{it['title']}\n內文：\n{it['body'][:per]}")
    return "\n\n".join(blocks)


def fmt_issues(issues):
    return "\n".join(f"- [{x.get('field')}] 「{x.get('wrong')}」→「{x.get('correct')}」：{x.get('reason')}" for x in issues)


def local_check(a, cluster, raw_src):
    """AI#2 之前的程式檢查。回傳 (fatal, soft, n_zh, copied, ratio)。"""
    zh, en = str(a.get("article_zh") or ""), str(a.get("article_en") or "")
    n_zh = han_len(zh)
    segs = copied_segments(zh, raw_src)
    ratio = shingle_ratio(zh, raw_src)
    copied = bool(segs) or ratio > 0.22
    fatal, soft = [], []
    if n_zh < 200:
        fatal.append(f"中文正文只有 {n_zh} 字，至少要 260 字（只能用原始報導裡已有的事實擴寫細節與背景，不可捏造任何原文沒有的內容）")
    if copied:
        fatal.append("以下片段與原文幾乎相同，請完全改用自己的句子重寫：" + "；".join(segs[:6]) + f"（重複比例 {ratio:.0%}）")
    if any(norm(a.get("title_zh", "")) == norm(it["title"]) for it in cluster):
        fatal.append("中文標題不可與原始標題相同，請換一個說法")
    for q in sorted({q for it in cluster for q in re.findall(r"[「『“\"]([^」』”\"]{2,12})[」』”\"]", it["title"])}):
        if q not in zh:
            fatal.append(f"原文的專有名稱「{q}」必須一字不改照抄（目前文章沒有出現或寫錯了）")
    zh_all = " ".join(str(a.get(k) or "") for k in ("title_zh", "summary_zh", "article_zh")) + " " + " ".join(map(str, a.get("tags") or []))
    en_all = " ".join(str(a.get(k) or "") for k in ("title_en", "summary_en", "article_en"))
    pov = sorted(set(TW_POV_ZH.findall(zh_all)))
    if pov:
        fatal.append("不可用「" + "、".join(pov) + "」這種站在台灣視角的代稱，一律明寫「台灣」（例如 本國銀行→台灣的銀行、國內→台灣、國人→台灣民眾）")
    pov_en = sorted({m.lower() for m in TW_POV_EN.findall(en_all)})
    if pov_en:
        fatal.append("英文版不可用 " + "、".join(pov_en) + "，請明寫 Taiwan／Taiwanese（例如 Taiwanese banks、Taiwan's market）")
    if len(en.split()) < 120:
        soft.append("英文正文太短，需要 180–320 個英文字")
    for zk, ek in GLOSSARY:
        if zk in zh and ek.lower() not in en.lower():
            soft.append(f"英文版「{zk}」要寫成 {ek}")
    return fatal, soft, n_zh, copied, ratio


def rewrite(cluster, prev=None, issues=None):
    """AI#1 撰稿。prev＋issues 有值時是依審稿意見修改。內含程式檢查（抄寫、字數、引號名稱、台灣視角代稱）。"""
    now = datetime.now(TZ).strftime("%Y 年 %m 月 %d 日 %H:%M")
    user = f"現在時間（台北）：{now}\n\n以下是同一事件的原始報導，請依規則改寫：\n\n{source_text(cluster)}"
    base = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    msgs = base
    if prev is not None:
        keep = {k: prev.get(k) for k in ("category", "title_zh", "title_en", "summary_zh", "summary_en", "article_zh", "article_en", "tags", "slug")}
        msgs = base + [{"role": "assistant", "content": json.dumps(keep, ensure_ascii=False)[:6000]},
                       {"role": "user", "content": "審稿編輯發現以下問題，請逐條修正（事實一律以原始報導為準；沒有被指出的內容盡量不要動），重新輸出完整 JSON（所有欄位都要有）：\n" + fmt_issues(issues)}]
    raw_src = "\n".join(it["title"] + "\n" + it["body"] for it in cluster)
    last_err = None
    expanded = recopied = json_retry = False  # 擴寫、抄寫退回、JSON 重試各最多一次
    for attempt in range(4):
        model, out = chat(msgs, temperature=(0.3 if prev is not None else 0.5) if attempt == 0 else 0.7)
        try:
            a = parse_json_obj(out)
        except Exception:
            last_err = "JSON 解析失敗"
            log(f"[writer] 第 {attempt + 1} 次 JSON 解析失敗：{str(out)[:200]!r}")
            if json_retry:
                break
            json_retry = True
            msgs = msgs + [{"role": "assistant", "content": str(out)[:3000]}, {"role": "user", "content": "請只輸出一個合法 JSON 物件，不要 ``` 也不要任何說明文字。"}]
            msgs = msgs[:2] + msgs[-4:] if len(msgs) > 8 else msgs
            continue
        if a.get("category") not in ("news", "tech"):
            a["category"] = (prev or {}).get("category") or cluster[0]["cat"]
        fatal, soft, n_zh, copied, ratio = local_check(a, cluster, raw_src)
        if not fatal and (not soft or attempt > 0):
            a["_model"] = model
            a["_ratio"] = round(ratio, 3)
            return a
        if (n_zh < 200 and expanded) or (copied and recopied):
            last_err = "；".join(fatal)
            break
        if n_zh < 200:
            expanded = True
        if copied:
            recopied = True
        last_err = "；".join(fatal + soft)
        log(f"[writer] 第 {attempt + 1} 次程式檢查不合格：{last_err[:300]}")
        msgs = base + [{"role": "assistant", "content": json.dumps(a, ensure_ascii=False)[:6000]}, {"role": "user", "content": "這篇不合格：" + last_err + "。請修正後重新輸出完整 JSON（所有欄位都要有）。"}]
    raise ValueError(last_err or "改寫失敗")


REVIEW_SYSTEM = """你是 Taiwan INFO 台灣資訊的資深審稿編輯（AI#2）。你會拿到「原始報導全文」與「改寫後的中英文章」（JSON）。請逐字對照原始報導，只找出真正的錯誤，不要改寫風格。

檢查項目：
1. 錯字、錯別字、同音字誤用（中文與英文都要看）。
2. 與原文不符的事實、數字、金額、百分比、日期、人名、職稱、機構名、地名；原文沒有的內容（捏造、推測）。
3. 「」引號內的名稱（活動、口號、政策、作品）必須與原文一字不差。
4. 英文專有名詞誤譯或自創：例如 民進黨 = Democratic Progressive Party（不是 Min-Hsin Party）、國民黨 = Kuomintang、民眾黨 = Taiwan People's Party、金管會 = Financial Supervisory Commission、中選會 = Central Election Commission、立法院 = Legislative Yuan、行政院 = Executive Yuan、新南向國家 = New Southbound Policy countries；本益比 = price-to-earnings (P/E) ratio（不是 earnings per share，EPS 是每股盈餘）；公司、機構不可自編英文名（例如 安聯投信 = Allianz Global Investors Taiwan，不是 Union Asset Management），不確定就用通行英文名或羅馬拼音。
5. 英文金額單位換算：1 億 = 100 million、10 億 = 1 billion、1 兆 = 1 trillion，且保留原數字精確度（811.8 億元 = NT$81.18 billion，不是 NT$81.1 billion；98.5 億元 = NT$9.85 billion；3.5 億元 = NT$350 million）。
6. 不可用站在台灣視角的代稱「本國」「我國」「國內」「本土」「國人」，一律明寫「台灣」；英文不可用 domestic、our country、local banks，要寫 Taiwan／Taiwanese（例如 Taiwanese banks、Taiwan's banks）。
7. 中英文版的事實要一致（英文版不可漏改數字或與中文矛盾）。

規則：
- 只列出確定的錯誤；風格、措辭、句子長短、同義說法（例如 a year earlier／year on year／from the same period last year 都對應「較去年同期」）、英文標題大小寫都不算錯。原文沒寫而文章也沒寫的不算錯。
- 確認正確的地方不要列出來（wrong 與 correct 相同的不要列）。
- 原文的「今年」「昨天」等相對日期，依原文發布時間換算成完整日期或年份是正確的，不算捏造。現在時間見下方；不要自己做民國／西元換算去推翻文章寫的年份。
- 「缺少資訊」「建議補充」「建議加上年份」「可以寫得更清楚」都不是錯誤，不要列出；只列文章「寫錯」的地方。
- wrong 必須是從文章該欄位「原樣複製」的一小段文字（5–60 字），correct 是改正後的文字，reason 用繁體中文簡短說明並指出原文依據。
- field 只能是 title_zh、title_en、summary_zh、summary_en、article_zh、article_en、tags 之一。
- 沒有錯誤時 pass 為 true、issues 為空陣列。

只輸出一個 JSON 物件（不要 ``` 也不要說明文字）：
{"pass": true 或 false, "issues": [{"field": "...", "wrong": "...", "correct": "...", "reason": "..."}]}"""

REVIEW_FIELDS = ("title_zh", "title_en", "summary_zh", "summary_en", "article_zh", "article_en", "tags")


def review(a, cluster):
    """AI#2 審稿：附原始報導全文。回傳 (pass, issues, model)。issues 只保留 wrong 真的出現在該欄位的項目。"""
    art = {k: a.get(k) for k in REVIEW_FIELDS}
    nw = datetime.now(TZ)
    user = f"現在時間（台北）：西元 {nw:%Y-%m-%d %H:%M}（民國 {nw.year - 1911} 年；原文的「今年」＝西元 {nw.year} 年）\n\n【原始報導全文】\n" + source_text(cluster, full=True) + "\n\n【改寫後的文章（JSON）】\n" + json.dumps(art, ensure_ascii=False, indent=1) + "\n\n請依審稿規則檢查，只輸出 JSON。"
    msgs = [{"role": "system", "content": REVIEW_SYSTEM}, {"role": "user", "content": user}]
    for attempt in range(2):
        model, out = chat(msgs, max_tokens=REVIEW_MAX_TOKENS, temperature=0.1, role="reviewer", thinking=REVIEW_THINKING)
        try:
            r = parse_json_obj(out)
            if not isinstance(r.get("pass"), bool) or not isinstance(r.get("issues", []), list):
                raise ValueError("格式不符")
        except Exception as e:
            log(f"[reviewer] 第 {attempt + 1} 次輸出無法解析（{e}）：{str(out)[:200]!r}")
            continue
        issues, dropped = [], []
        for x in r.get("issues") or []:
            if not isinstance(x, dict):
                continue
            f, w, c = str(x.get("field") or ""), str(x.get("wrong") or "").strip(), str(x.get("correct") or "").strip()
            text = " ".join(map(str, a.get(f) or [])) if f == "tags" else str(a.get(f) or "")
            if f not in REVIEW_FIELDS or not w or w == c or w not in text:
                dropped.append(x)
                continue
            issues.append({"field": f, "wrong": w, "correct": c, "reason": str(x.get("reason") or "")})
        if dropped:
            log(f"[reviewer] 忽略 {len(dropped)} 條無法對應文章原文的意見：{json.dumps(dropped, ensure_ascii=False)[:300]}")
        return (not issues), issues, model
    raise ValueError("審稿模型輸出無法解析")


def reverts(history):
    """最新一輪的意見是否要求改回前面某輪被指為錯的寫法（審稿模型自相矛盾）。"""
    if len(history) < 2:
        return False
    old_wrong = {x["wrong"] for h in history[:-1] for x in h["issues"]}
    return any(x["correct"] in old_wrong for x in history[-1]["issues"])


def write_and_review(cluster, max_rounds=3):
    """AI#1 撰稿（含程式檢查）→ AI#2 審稿；有錯退回 AI#1 修改再審，最多 max_rounds 輪審稿。回傳 (文章, 審稿紀錄)；不通過丟 ValueError。"""
    a = rewrite(cluster)
    history, seen = [], set()
    for rnd in range(1, max_rounds + 1):
        ok, issues, rm = review(a, cluster)
        history.append({"round": rnd, "pass": ok, "issues": issues, "model": rm})
        if ok:
            log(f"[review] 第 {rnd} 輪通過（{rm}）")
            a["_review"] = history
            return a, history
        log(f"[review] 第 {rnd} 輪退回（{rm}），{len(issues)} 條：\n" + fmt_issues(issues))
        key = json.dumps(sorted((x["field"], x["wrong"]) for x in issues), ensure_ascii=False)
        if key in seen:
            log("[review] 同樣的問題第二次出現，AI#1 修不好，停止")
            break
        if reverts(history):
            log("[review] 審稿意見前後矛盾（要求改回上一輪的寫法），停止")
            raise ValueError("審稿意見前後矛盾：" + fmt_issues(issues)[:300])
        seen.add(key)
        if rnd == max_rounds:
            break
        a = rewrite(cluster, prev=a, issues=issues)
        still = [x for x in issues if x["wrong"] not in x["correct"] and x["wrong"] in (" ".join(map(str, a.get(x["field"]) or [])) if x["field"] == "tags" else str(a.get(x["field"]) or ""))]
        if still:
            log(f"[writer] 修改後仍保留 {len(still)} 處被指出的文字：" + "；".join(x["wrong"] for x in still)[:200])
    raise ValueError(f"審稿 {len(history)} 輪仍不通過：" + fmt_issues(history[-1]["issues"])[:400])


# ---------------- 主流程 ----------------
def main():
    t0 = time.time()
    with cf.ThreadPoolExecutor(8) as ex:
        items = [x for r in ex.map(fetch_feed, FEEDS) for x in r]
    now = datetime.now(TZ)
    items = [x for x in items if x["published"] is None or now - x["published"] < timedelta(hours=MAX_AGE_H)]
    # 同網址只留一筆
    seen, uniq = set(), []
    for x in sorted(items, key=lambda x: -x["w"]):
        if x["url"] not in seen:
            seen.add(x["url"])
            uniq.append(x)
    log(f"RSS：{len(items)} 則（{MAX_AGE_H} 小時內），不同網址 {len(uniq)}")

    recent = [] if DRY and not os.environ.get("NEWS_INGEST_TOKEN") else site_recent()
    known_urls = {canon(s["url"]) for r in recent for s in r["sources"]}
    known_titles = [r["title_zh"] for r in recent] + [s["title"] for r in recent for s in r["sources"] if s.get("title")]
    fresh = [x for x in uniq if x["url"] not in known_urls and not any(sim(x["title"], t) >= SAME_EVENT for t in known_titles)]
    log(f"網站已收錄 {len(recent)} 篇；未收錄的新聞 {len(fresh)} 則")

    # 合併同一事件
    clusters = []
    for x in sorted(fresh, key=lambda x: x["published"] or now, reverse=True):
        for c in clusters:
            if any(sim(x["title"], y["title"]) >= SAME_EVENT for y in c):
                if x["source"] not in {y["source"] for y in c} and len(c) < 3:
                    c.append(x)
                break
        else:
            clusters.append([x])

    def score(c):
        newest = max((y["published"] or now) for y in c)
        age_h = (now - newest).total_seconds() / 3600
        return len({y["source"] for y in c}) * 4 + max(y["w"] for y in c) - age_h * 0.8

    clusters.sort(key=score, reverse=True)
    n = MAX_ARTICLES + (1 if len(clusters) >= 25 and MAX_ARTICLES < 3 else 0)
    today = now.strftime("%Y-%m-%d")
    done_today = sum(1 for r in recent if str(r.get("published_at", "")).startswith(today))
    allowed = int(DAILY_CAP * (now.hour + now.minute / 60 + 1) / 24) - done_today
    if allowed <= 0:
        log(f"今天已發 {done_today} 篇，依每日上限 {DAILY_CAP} 的時間配額，本次不改寫")
        return
    n = min(n, allowed)
    picks = clusters[: n * 2]  # 多抓一些候選，原文太短的跳過
    for c in picks[:n * 2]:
        log(f"候選（{score(c):.1f}）：" + " | ".join(f"{y['source']}:{y['title']}" for y in c))
    if DRY:
        return

    with cf.ThreadPoolExecutor(6) as ex:
        bodies = list(ex.map(fetch_body, [y for c in picks for y in c]))
    k = 0
    for c in picks:
        for y in c:
            y["body"] = bodies[k]
            k += 1

    out, done, skipped = [], 0, []
    for c in picks:
        if done >= n:
            break
        if time.time() - t0 > TIME_BUDGET:
            log(f"[time] 已用 {time.time() - t0:.0f} 秒，超過 {TIME_BUDGET} 秒預算，剩下的留給下一次")
            break
        c = [y for y in c if han_len(y["body"]) >= 80] or c
        if sum(han_len(y["body"]) for y in c) < 150:
            log(f"[skip] 原文太短：{c[0]['title']}")
            continue
        try:
            a, hist = write_and_review(c)
        except QuotaExhausted as e:
            log(f"[quota] {e}；本次跳過")
            break
        except Exception as e:
            log(f"[skip] 不發布：{c[0]['title']}：{e}")
            skipped.append((c[0]["title"], str(e)[:200]))
            continue
        urls = sorted(y["url"] for y in c)
        sh = hashlib.sha256("\n".join(urls).encode()).hexdigest()[:24]
        pub = now.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        slug = re.sub(r"[^a-z0-9]+", "-", str(a.get("slug") or a.get("title_en") or "").lower()).strip("-")[:60].strip("-")
        art = {
            "id": f"{now:%Y%m%d}-{sh[:8]}", "slug": slug or None, "category": a["category"],
            "title_zh": a["title_zh"].strip(), "title_en": (a.get("title_en") or "").strip() or None,
            "summary_zh": (a.get("summary_zh") or "").strip() or None, "summary_en": (a.get("summary_en") or "").strip() or None,
            "article_zh": a["article_zh"].strip(), "article_en": (a.get("article_en") or "").strip() or None,
            "tags": [str(t) for t in (a.get("tags") or [])][:6],
            "sources": [{"name": y["source"], "title": y["title"], "url": y["url"], "published_at": y["published"].isoformat() if y["published"] else None} for y in c],
            "source_hash": sh, "published_at": pub, "model": a["_model"],
        }
        log(f"[ok] {art['id']} {art['title_zh']}（{a['_model']}，中文 {han_len(art['article_zh'])} 字，與原文 8 字重複比例 {a['_ratio']:.0%}，審稿 {len(hist)} 輪通過，退回 {len(hist) - 1} 次）")
        r = site_post([art])
        log(f"[post] {json.dumps(r, ensure_ascii=False)[:300]}")
        if r.get("ok"):
            out.append(art)
            done += 1
    summary = f"本次新增 {len(out)} 篇、審稿不通過跳過 {len(skipped)} 篇，用時 {time.time() - t0:.0f} 秒"
    log(summary)
    gs = os.environ.get("GITHUB_STEP_SUMMARY")
    if gs:
        with open(gs, "a") as f:
            f.write(f"### {summary}\n\n" + "".join(f"- [{a['title_zh']}]({SITE}/articles/{a['id']})（{a['model']}）\n" for a in out)
                    + "".join(f"- 跳過：{t}（{r}）\n" for t, r in skipped))


if __name__ == "__main__":
    main()
