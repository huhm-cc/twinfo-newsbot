# twinfo-newsbot

[Taiwan INFO 台灣資訊](https://twinfo.huhm.cc) 的即時新聞機器人。GitHub Actions 每 10 分鐘執行一次：

1. 讀取台灣新聞的公開 RSS（中央社、公視、自由時報、科技新報、iThome、INSIDE）。
2. 用標題相似度把同一事件的多家報導合併，並排除網站已收錄的新聞。
3. 每次挑 2–3 則新事件，讀取原文後用 GitHub Models 以中立語氣**重新撰寫**成中英雙語文章（不逐句翻抄、不大段引用，事實、數字、人名以原文為準），並自動檢查與原文的重複程度。
4. 送到網站，刊登在 [twinfo.huhm.cc/articles](https://twinfo.huhm.cc/articles)，每篇附原始報導連結；開發者可透過 [新聞 API](https://twinfo.huhm.cc/developers) 取得。

網站端用 GitHub Actions OIDC 驗證這個 repo 的 main 分支，不需要任何 repo secret。GitHub Models 免費額度用完時，該次執行會直接跳過，不算失敗。

本地測試：`DRY_RUN=1 python newsbot.py`（只列出會挑選的新聞，不呼叫模型、不寫入）。

---

Taiwan INFO's newsroom bot. Every 10 minutes it reads public Taiwanese news RSS feeds, groups reports about the same event, rewrites the newest stories in neutral Chinese and English with GitHub Models (no copying of original sentences; facts, figures and names kept as reported), and publishes them at twinfo.huhm.cc/articles with links to the original reports.
