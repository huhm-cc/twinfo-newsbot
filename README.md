# twinfo-newsbot

[Taiwan INFO 台灣資訊](https://twinfo.huhm.cc) 的即時新聞機器人。GitHub Actions 每 10 分鐘執行一次：

1. 讀取台灣新聞的公開 RSS（中央社、公視、自由時報、科技新報、iThome、INSIDE）。
2. 用標題相似度把同一事件的多家報導合併，並排除網站已收錄的新聞。
3. 每次挑 2–3 則新事件，讀取原文後用兩階段 AI（經網站 `/api/news/rewrite` 代理，NVIDIA API）：
   - **AI#1 撰稿**（`nvidia/nemotron-3.5-bolt-e4b`）：以中立語氣**重新撰寫**成中英雙語文章（不逐句翻抄、不大段引用，事實、數字、人名以原文為準，一律明寫「台灣」不用「本國／國內」等代稱）。
   - **程式檢查**：與原文的重複程度、中文字數、標題引號內名稱、台灣視角代稱。
   - **AI#2 審稿**（`nvidia/nemotron-3-ultra-550b-a55b`，附原始報導全文）：檢查錯字、與原文不符的事實／數字／人名、英文專有名詞誤譯、億／兆換算。有錯就把意見退回 AI#1 修改再審，最多 3 輪；仍不通過就不發布。
4. 送到網站，刊登在 [twinfo.huhm.cc/articles](https://twinfo.huhm.cc/articles)，每篇附原始報導連結；開發者可透過 [新聞 API](https://twinfo.huhm.cc/developers) 取得。

網站端用 GitHub Actions OIDC 驗證這個 repo 的 main 分支，不需要任何 repo secret。LLM 額度用完或尚未開通時，該次執行會直接跳過，不算失敗。

本地測試：`DRY_RUN=1 python newsbot.py`（只列出會挑選的新聞，不呼叫模型、不寫入）。重審已發布文章：`python recheck.py [id ...]`（`REVIEW_ONLY=1` 只審不改）。

---

Taiwan INFO's newsroom bot. Every 10 minutes it reads public Taiwanese news RSS feeds, groups reports about the same event, rewrites the newest stories in neutral Chinese and English with a writer LLM, has a second reviewer LLM check them against the full original reports (up to 3 fix rounds, otherwise skipped) (no copying of original sentences; facts, figures and names kept as reported), and publishes them at twinfo.huhm.cc/articles with links to the original reports.
