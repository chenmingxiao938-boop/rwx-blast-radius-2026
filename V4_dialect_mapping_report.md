# V4 Phase 2：動態方言依賴研究

固定來源：sqlglot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`。正式測試編號來自 `sample/tests.json`；未讀取 `sample/answers.json`，未修改正式選測、Docker、排程或 Phase 1 原型。完整逐候選證據與未解決訊號可由 `audit_dialect_impact.py` 重現，結果摘要在 `V4_dialect_results.json`。

## 派送鏈

`sqlglot/dialects/__init__.py:74-120` 的 `DIALECTS` 清單建立模組名稱，`__getattr__` 透過 `importlib.import_module` 延遲載入。`sqlglot/dialects/dialect.py:218-290` 的 `_Dialect` 註冊並按名稱載入方言類別；`Dialect.get_or_raise` 在 1092-1147 行解析名稱。`sqlglot/__init__.py:136-169,171-204` 的 `parse_one`／`transpile` 使用此查找。`Dialect.parse`／`generate` 最終分別建立 `parser_class`／`generator_class`（`dialect.py:1346-1379`）；類別在 `_Dialect.__new__` 由方言模組的 `Parser`／`Generator` 指派（`dialect.py:334-348`）。

測試端 `tests/dialects/test_dialect.py:26-105` 的 `Validator` 使用 `self.dialect` 調用 `parse_one`，並按 `read`／`write` 字典進行解析和產生；`TestSingleStore` 與 `TestMaterialize` 都繼承它。這條動態鏈不會出現在測試檔的直接 Python import 邊中，解釋了原圖對 SingleStore parser 和 Materialize 方言模組的 0 候選。

原型 `map_dialect_impacts(changed_paths, test_metadata, source_root)` 從固定版 `DIALECTS` 清單和各方言類別指派建立模組關係，再掃描正式註冊的測試方法。結果含候選 ID／編號、每條證據的測試檔行號、方言、解析或產生角色、相關實作模組、全域與逐檔未解決原因；`fallback_required` 始終為 `True`。靜態來源證據與執行時觀察分開記錄。

## 三個指定模組

| 模組 | 原 import 圖候選 | 動態證據候選 | 原圖缺少的候選 | 合併後 | 判斷 |
| --- | ---: | ---: | ---: | ---: | --- |
| `sqlglot/parsers/singlestore.py` | 0 | 30 | 30 | 30 | 28 個 SingleStore 測試，加上 `test_dialect::test_json_keys` 與 `test_mysql::test_statistical_aggregates` |
| `sqlglot/dialects/materialize.py` | 0 | 10 | 10 | 10 | 2 個 Materialize 測試，另 8 個測試透過字面 `read`／`write` 或解析派送使用它 |
| `sqlglot/parsers/bigquery.py` | 1474 | 188 | 0 | 1474 | 動態掃描補充了 188 個具體派送證據，但沒有增加合併候選 |

SingleStore 代表證據：`tests/dialects/test_singlestore.py:7,31-32` 的 `dialect = "singlestore"` 與 `self.validate_identity`，透過 `Validator` 的 `self.parse_one` 解析；`test_dialect.py:6048` 的 `read` 字典與 `test_mysql.py:195` 的 `read` 字典另提供跨檔讀取證據。固定版 `sqlglot/dialects/singlestore.py:5,68` 把 `SingleStoreParser` 接到方言。30 個候選均是原圖沒有的正面連結。

Materialize 代表證據：`tests/dialects/test_materialize.py:4-8,58-59` 的繼承與 `self.dialect`，`tests/dialects/test_bigquery.py:189`、`tests/dialects/test_spark.py:118` 和多個 `test_dialect.py` 的字面 `write={"materialize": ...}`。固定版 `sqlglot/dialects/materialize.py:8,16-18` 指派其 Parser／Generator。這些是直接測試呼叫證據；同一測試內其他 SQL 路徑仍未完整解析。

BigQuery 代表證據：`tests/dialects/test_bigquery.py:22-23` 的繼承方言屬性，以及 `tests/test_optimizer.py` 中多處字面 `read="bigquery"`。固定版 `sqlglot/dialects/bigquery.py:9,192` 接到 `BigQueryParser`。原 import 圖已經包含所有 188 個動態證據候選，因此這一例改善的是可解釋性，沒有改善合併召回數；1474 個圖候選也不表示精準或完整。

受控 Python 3.13 執行 `trace_dialect_dispatch.py`，三種名稱分別得到 `SingleStoreParser`／`SingleStoreGenerator`、`MaterializeParser`／`MaterializeGenerator`、`BigQueryParser`／`BigQueryGenerator`，且 `parse_one` 和 `transpile` 成功。這驗證了代表性的執行時類別選擇，沒有執行全部測試，也沒有證明全部可達路徑已枚舉。

## 未解決風險與資源

結果把 122 個全域診斷訊號與每檔 1 個未證明完整性標記分開：全域包含 81 個計算出的方言參數、33 個計算出的方言集合、2 個動態 import、6 個未知方言名稱。它們來自整個測試樹，並非都已證明與每個目標模組有關；名稱無法靜態解析時，亦不能判定其無關。原型沒有完整追蹤 `self.dialect` 的多層繼承、別名及重新賦值、fixture 內容中的方言值、插件註冊、自訂載入器或 SQLGlot 所有跨模組間接呼叫。對 `.sql(...)`、迴圈字面集合的辨識是來源支持的候選近似，不能當作完整執行覆蓋。

研究分析一次掃描三個模組，包含載入依賴圖、分析測試 AST 與輸出 JSON，共耗 CPU **453.025 ms**，程序峰值 RSS **43,244 KiB**。這是離線原型成本；若接入 Shadow Mode，應在建置時預先生成固定版方言和測試證據映射，避免每批重掃。Python 3.13 編譯四個新檔成功；相關單元測試 **102 通過、1 跳過、0 失敗**。

**結論：**可作為 Shadow Mode 的正面候選與解釋資料。三類修改都尚無足夠完整的影響證據支持實驗性排除測試；繼續保留全套回退。下一步只應針對新增的 SingleStore／Materialize 關係做逐測試執行追蹤與未解決方言參數驗證，不需要重寫依賴圖。
