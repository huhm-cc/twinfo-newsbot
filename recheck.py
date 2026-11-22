#!/usr/bin/env python3
"""用 AI#2 重審網站上已發布的文章：有錯退回 AI#1 修改（最多 3 輪審稿），通過就 PUT 更新；修不好就 DELETE 下架。
用法：python recheck.py [id ...]（沒給 id 就重審最近 200 篇）；REVIEW_ONLY=1 只審不改。環境變數同 newsbot.py。"""
import json
import os
import sys

import newsbot as nb


def site_get(id):
    _, b = nb.http(f"{nb.SITE}/api/news/ingest?id={id}", headers=nb.site_auth())
    return json.loads(b)["item"]


def site_put(a):
    data = json.dumps(a, ensure_ascii=False).encode()
    try:
        _, b = nb.http(f"{nb.SITE}/api/news/ingest", data=data, method="PUT", headers={**nb.site_auth(), "content-type": "application/json"}, timeout=40)
        return json.loads(b)
    except nb.urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode("utf-8", "ignore")[:300]}


def site_delete(id):
    _, b = nb.http(f"{nb.SITE}/api/news/ingest?id={id}", method="DELETE", headers={**nb.site_auth(), "content-type": "application/json"})
    return json.loads(b)


def recheck(id, review_only=False):
    cur = site_get(id)
    cluster = []
    for s in cur["sources"]:
        it = {"source": s.get("name") or "", "title": s.get("title") or "", "url": s["url"], "published": nb.parse_date(s.get("published_at")), "cat": cur["category"], "desc": ""}
        it["body"] = nb.fetch_body(it)
        cluster.append(it)
    if sum(nb.han_len(y["body"]) for y in cluster) < 150:
        return {"id": id, "result": "原文抓不到，無法重審"}
    a = {k: cur.get(k) for k in ("category", "slug", "title_zh", "title_en", "summary_zh", "summary_en", "article_zh", "article_en", "tags")}
    hist, seen = [], set()
    for rnd in range(1, 4):
        ok, issues, rm = nb.review(a, cluster)
        hist.append({"round": rnd, "pass": ok, "issues": issues})
        if ok:
            nb.log(f"[recheck] {id} 第 {rnd} 輪通過（{rm}）")
            if rnd == 1:
                return {"id": id, "result": "原文無誤", "history": hist}
            r = site_put({"id": id, **{k: a.get(k) for k in ("category", "title_zh", "title_en", "summary_zh", "summary_en", "article_zh", "article_en", "tags")}, "model": a.get("_model") or cur.get("model")})
            return {"id": id, "result": "已修正更新" if r.get("ok") else f"更新失敗 {r}", "history": hist, "new": a}
        nb.log(f"[recheck] {id} 第 {rnd} 輪退回，{len(issues)} 條：\n" + nb.fmt_issues(issues))
        if review_only:
            return {"id": id, "result": "有錯（只審不改）", "history": hist}
        if nb.reverts(hist):
            return {"id": id, "result": "審稿意見前後矛盾，保留網站上的原版（未更新、未下架）", "history": hist}
        key = json.dumps(sorted((x["field"], x["wrong"]) for x in issues), ensure_ascii=False)
        if key in seen or rnd == 3:
            break
        seen.add(key)
        a = nb.rewrite(cluster, prev=a, issues=issues)
    r = site_delete(id)
    return {"id": id, "result": f"修不好，已下架 {r}", "history": hist}


def main():
    ids = sys.argv[1:] or [r["id"] for r in nb.site_recent()]
    res = []
    for id in ids:
        try:
            res.append(recheck(id, os.environ.get("REVIEW_ONLY") == "1"))
        except Exception as e:
            res.append({"id": id, "result": f"錯誤：{e}"})
        nb.log(f"[recheck] {id}：{res[-1]['result']}")
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
