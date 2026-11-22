#!/usr/bin/env python3
"""Taiwan INFO newsbot：抓台灣新聞 RSS → 去重、合併同一事件 → 用 GitHub Models 改寫成中英文章 → POST 到 twinfo.huhm.cc。

只用 Python 標準函式庫。環境變數：
  GITHUB_TOKEN            GitHub Models 用（Actions 內 permissions: models: read）
  NEWS_INGEST_TOKEN       （可選）網站寫入 token；沒有時改用 GitHub Actions OIDC（permissions: id-token: write）
  SITE                    預設 https://twinfo.huhm.cc
  MAX_ARTICLES            每次最多改寫幾則（預設 2，新事件很多時 3）
  MODELS                  逗號分隔，依序嘗試（遇到額度用完 429 換下一個，全部用完就跳過，不算失敗）
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
MODELS = [m.strip() for m in os.environ.get("MODELS", "openai/gpt-4.1-mini,openai/gpt-4o-mini,openai/gpt-4.1-nano").split(",") if m.strip()]
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "2"))
DRY = os.environ.get("DRY_RUN") == "1"
MAX_AGE_H = 8  # 只處理 8 小時內的新聞

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


def chat(messages, max_tokens=2600, temperature=0.4):
    tok = os.environ.get("GITHUB_TOKEN", "")
    for m in MODELS:
        if m in dead_models:
            continue
        body = json.dumps({"model": m, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "response_format": {"type": "json_object"}}).encode()
        for attempt in range(2):
            try:
                st, b = http("https://models.github.ai/inference/chat/completions", data=body, method="POST", timeout=120,
                            headers={"Authorization": f"Bearer {tok}", "content-type": "application/json", "Accept": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
                try:
                    j = json.loads(b)
                except Exception:
                    log(f"[model] {m} 回應不是 JSON（HTTP {st}）：{b[:300]!r}")
                    raise
                return m, j["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "ignore")[:300]
                log(f"[model] {m} HTTP {e.code}: {msg}")
                if e.code == 429:
                    ra = int(e.headers.get("retry-after", "0") or 0)
                    if 0 < ra <= 65 and attempt == 0:  # 每分鐘限制：等一下再試
                        time.sleep(ra)
                        continue
                    dead_models.add(m)  # 每日額度用完
                    break
                if e.code in (401, 403, 404, 400, 413):
                    dead_models.add(m)
                    break
                time.sleep(3)
            except Exception as e:
                log(f"[model] {m} 錯誤：{e}")
                time.sleep(3)
    raise QuotaExhausted("所有模型都無法使用（多半是 GitHub Models 每日免費額度用完）")


SYSTEM = """你是 Taiwan INFO 台灣資訊的新聞編輯。你會拿到同一則新聞事件的一或多篇原始報導，請「用自己的話」重新寫成一篇新的新聞稿，避免任何著作權爭議。

改寫規則（一定要遵守）：
1. 不可逐句翻抄或只換幾個字；要重新組織結構與句子，用你自己的措辭。不可大段引用原文；直接引語最多一句、且不超過 20 字並註明是誰說的，其餘一律改為轉述。
2. 只能使用原始報導裡有的事實。人名、職稱、機構名、地名、數字、金額、百分比、日期照原文保留，不可更改、不可推測、不可補充原文沒有的內容。
3. 中立客觀：不加評論、不下結論、不用情緒或煽動字眼、不臆測；有爭議時並陳各方說法。不寫「本報」「記者」「綜合報導」等字樣。
4. 中文用繁體中文與台灣用語、全形標點。日期寫成完整日期（例如 2026 年 11 月 22 日），不要只寫「今天」「22 日」。
5. 中文正文 3–5 段，段落之間用 \\n\\n 分隔，總長 260–450 字（不含標點），第一段是導言（誰、什麼事、何時、何地）。
6. 英文正文是同一篇的英文版（不是逐字翻譯也可），3–5 段，180–320 個英文字，使用一般國際媒體寫法（例如 Taiwan's Central Election Commission、New Taiwan dollars (NT$)）。
7. 不要在文末列出來源、不要 hashtag、不要 emoji、不要問句互動。

只輸出一個 JSON 物件，欄位：
{"category": "news" 或 "tech"（科技、AI、半導體、資安、網路、電子產業歸 tech，其餘 news）,
 "title_zh": "中文標題，14–30 字，不可與任何原始標題相同",
 "title_en": "English headline",
 "summary_zh": "一句話中文摘要，40–80 字",
 "summary_en": "One-sentence English summary",
 "article_zh": "中文正文",
 "article_en": "English article",
 "tags": ["3–6 個中文關鍵字"],
 "slug": "english-url-slug-3-to-8-words"}"""


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


def rewrite(cluster):
    src_blocks, total = [], 0
    per = max(1200, 4800 // len(cluster))
    for i, it in enumerate(cluster, 1):
        txt = it["body"][:per]
        total += len(txt)
        pub = it["published"].strftime("%Y-%m-%d %H:%M") if it["published"] else "不明"
        src_blocks.append(f"【報導 {i}】來源：{it['source']}｜發布時間（台北）：{pub}\n標題：{it['title']}\n內文：\n{txt}")
    joined = "\n\n".join(src_blocks)
    now = datetime.now(TZ).strftime("%Y 年 %m 月 %d 日 %H:%M")
    user = f"現在時間（台北）：{now}\n\n以下是同一事件的原始報導，請依規則改寫：\n\n{joined}"
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    raw_src = "\n".join(it["title"] + "\n" + it["body"] for it in cluster)
    last_err = None
    for attempt in range(2):
        model, out = chat(msgs, temperature=0.5 if attempt == 0 else 0.7)
        try:
            a = json.loads(out)
        except Exception:
            last_err = "JSON 解析失敗"
            msgs = msgs[:2] + [{"role": "assistant", "content": out[:3000]}, {"role": "user", "content": "請只輸出合法 JSON。"}]
            continue
        probs = []
        zh, en = str(a.get("article_zh") or ""), str(a.get("article_en") or "")
        if han_len(zh) < 220:
            probs.append(f"中文正文只有 {han_len(zh)} 字，需要 260–450 字（只能用原文已有事實擴寫背景，不可捏造）")
        if len(en.split()) < 120:
            probs.append("英文正文太短，需要 180–320 字")
        segs = copied_segments(zh, raw_src)
        ratio = shingle_ratio(zh, raw_src)
        if segs or ratio > 0.22:
            probs.append("以下片段與原文幾乎相同，請完全改用自己的句子重寫：" + "；".join(segs[:6]) + f"（重複比例 {ratio:.0%}）")
        if any(norm(a.get("title_zh", "")) == norm(it["title"]) for it in cluster):
            probs.append("中文標題不可與原始標題相同")
        if a.get("category") not in ("news", "tech"):
            a["category"] = cluster[0]["cat"]
        if not probs:
            a["_model"] = model
            a["_ratio"] = round(ratio, 3)
            return a
        last_err = "；".join(probs)
        log(f"[rewrite] 第 {attempt + 1} 次不合格：{last_err[:300]}")
        msgs = msgs[:2] + [{"role": "assistant", "content": out[:4000]}, {"role": "user", "content": "這篇不合格：" + last_err + "。請修正後重新輸出完整 JSON。"}]
    raise ValueError(last_err or "改寫失敗")


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

    out, done = [], 0
    for c in picks:
        if done >= n:
            break
        c = [y for y in c if han_len(y["body"]) >= 80] or c
        if sum(han_len(y["body"]) for y in c) < 150:
            log(f"[skip] 原文太短：{c[0]['title']}")
            continue
        try:
            a = rewrite(c)
        except QuotaExhausted as e:
            log(f"[quota] {e}；本次跳過")
            break
        except Exception as e:
            log(f"[skip] 改寫失敗：{c[0]['title']}：{e}")
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
        log(f"[ok] {art['id']} {art['title_zh']}（{a['_model']}，中文 {han_len(art['article_zh'])} 字，與原文 8 字重複比例 {a['_ratio']:.0%}）")
        r = site_post([art])
        log(f"[post] {json.dumps(r, ensure_ascii=False)[:300]}")
        if r.get("ok"):
            out.append(art)
            done += 1
    summary = f"本次新增 {len(out)} 篇，用時 {time.time() - t0:.0f} 秒"
    log(summary)
    gs = os.environ.get("GITHUB_STEP_SUMMARY")
    if gs:
        with open(gs, "a") as f:
            f.write(f"### {summary}\n\n" + "".join(f"- [{a['title_zh']}]({SITE}/articles/{a['id']})（{a['model']}）\n" for a in out))


if __name__ == "__main__":
    main()
