# V4 Phase 3：依賴圖精度研究

固定來源：sqlglot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`；正式編號來自 `sample/tests.json`。本研究未讀取答案檔、未修改正式選測或 Docker，所有結果保持完整測試回退。可用 `audit_graph_precision.py` 重現 `V4_graph_precision_results.json`。

## 候選擴張的原因

`dependency_map.py:309-352` 對每個 Python 檔建立模組 import 邊，並加入父套件邊。`DependencyMap.analyze()` 在 272-290 行從修改檔反向走到測試檔；一旦測試檔可達，該檔全部註冊測試都進候選。這能涵蓋匯入時錯誤，卻把單一測試方法中的方言字面參數提升成整個測試檔的依賴。

最清楚的鏈是 `tests/dialects/test_athena.py:7` 匯入共用 `Validator` → `tests/dialects/test_dialect.py:212` 在某個方法中使用 ClickHouse → `sqlglot/dialects/clickhouse.py:9,150` 指派 `ClickHouseGenerator`。此鏈的中段只在特定方法執行時發生，不能據此認定所有 Athena 測試都會呼叫 ClickHouse。受控執行 `trace_graph_paths.py`：匯入共用 `Validator` 後 ClickHouse 和 SQLite 模組仍未載入；執行 `parse_one(..., read="clickhouse")` 後 ClickHouse 才載入。

BigQuery 有不同風險：共用 `test_dialect.py:15` 在檔案匯入時執行 `from sqlglot.dialects import BigQuery`。受控執行觀察到匯入 `Validator` 後 BigQuery 已載入。若 BigQuery parser 的修改造成匯入錯誤，引用此共用測試檔的其他測試也可能失敗。這條匯入時鏈不能按函式層級字面使用簡單刪除。

## 四個模組的結果

「證據子集」只包含目前原型能追到符號使用、類別繼承、方言指派、字面動態 import 或 Phase 2 方言證據的測試；它**不是安全選測集合**。「保留待驗證」仍在合併候選中，不能直接刪除。

| 模組 | 原圖候選 | 直接測試 | 間接測試 | 動態證據 | 動態新增 | 合併候選 | 證據子集 | 保留待驗證 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `generators/clickhouse.py` | 1392 | 0 | 1392 | 107 | 0 | 1392 | 109 | 1283 |
| `generators/sqlite.py` | 369 | 0 | 369 | 93 | 51 | 420 | 93 | 327 |
| `parsers/bigquery.py` | 1474 | 0 | 1474 | 188 | 0 | 1474 | 1055 | 419 |
| `dialects/dialect.py` | 1617 | 112 | 1505 | 尚未完整枚舉 | — | 1617 | 1598 | 19 |

- ClickHouse generator 的唯一直接 import 者是 `sqlglot/dialects/clickhouse.py`；其 `Generator = ClickHouseGenerator` 指派有來源證據。圖上另有 40 個間接可達檔，主要透過共用測試檔放大。目標類別繼承通用 `generator.Generator`，但反向第一跳沒有子類別使用者。保留 1283 個未獲完整函式層證據的候選。
- SQLite generator 的唯一直接 import 者是 `sqlglot/dialects/sqlite.py`，有 `Generator = SQLiteGenerator` 指派；間接可達檔僅 4 個。Phase 2 的 `write={"sqlite": ...}`、`Expr.sql("sqlite")` 等來源證據新增原圖漏掉的 51 個註冊測試，例如 `tests/dialects/test_bigquery.py:838`。這是召回缺口，不是精度改善。目標類別繼承通用 `generator.Generator`。
- BigQuery parser 的唯一直接 import 者是 `sqlglot/dialects/bigquery.py`，有 `Parser = BigQueryParser` 指派；間接可達檔 41 個。1055 個證據候選中包含前述共用 `Validator` 的匯入時鏈。其餘 419 個仍無可用的排除證明。目標類別繼承通用 `parser.Parser`。
- 核心 `dialects/dialect.py` 有 110 個直接 import 者（105 個正式程式檔、5 個測試檔），其中 22 個第一跳直接作為類別基底使用；另有 133 個間接可達檔。`sqlglot/__init__.py:17,60` 直接匯入核心方言符號，故 1617／1620 候選廣泛但有實際公共 API 與匯入風險。核心模組的動態分派使用者尚未完整枚舉。

上述四條目標的第一跳分別為 1、1、1、110 個直接 import 者；原型保留了第一跳來源行號、符號與五條代表性完整鏈。`graph_precision.py` 的 `analyze_graph_precision(changed_paths, metadata, graph, source_root, ...)` 回傳逐檔候選、鏈、邊種類和未解決訊號；它沒有移除任何原圖邊。

## 不確定性與限制

固定圖共有 319 條靜態警告；現有 `analyze()` 對四個修改模組各傳播 315 條，因為它先從**全部註冊測試**走可達性，再收集警告（`dependency_map.py:270-271`）。其中不在各目標已知反向依賴路徑上的警告數依序為 ClickHouse 163、SQLite 241、BigQuery 158、核心 Dialect 1。這是診斷重複，並不證明那些警告與修改無關；計算出的方言、動態載入與未知依賴仍需保留風險。Phase 2 另有 122 條全域方言未解決訊號，與逐檔訊號分列。

本原型的符號使用是 AST 層的字面證據，不等於該方法在測試中執行；最短 import 鏈也不是唯一可能鏈。它未對每個測試方法建立完整呼叫圖、處理所有類別覆寫或用 patch hunk 證明修改符號的唯一消費者。匯入時副作用、語法錯誤和測試檔匯入失敗可使看似「未使用符號」的測試受影響。因此本輪**沒有任何有證據支持的圖邊刪除**；「證據子集」之外的測試全留在候選中。若建圖時原始碼缺失或語法錯誤，原圖可能丟失邊；全套回退仍是必要保護。

## 成本、驗證與決定

離線審計四檔含 Phase 2 動態對應與 JSON 輸出，CPU **4009.813 ms**、程序峰值 RSS **220,312 KiB**。現有壓縮依賴圖 5,766 bytes；本輪結果 JSON 約 74 KB。AST 快取佔大部分記憶體，此分析不適合逐批放進正式執行；若只作 Shadow Mode 診斷，應先在建置期預算。Python 3.13 新增原型四檔編譯成功；相關單元測試 **118 通過、1 跳過、0 失敗**。

下一個最小實驗：只追蹤 ClickHouse 與 SQLite 的少數註冊測試在**檔案匯入和方法執行**兩階段實際載入哪些模組、呼叫哪些方言類別，並核對對應 patch hunk 的符號。若不能同時封閉匯入失敗與動態呼叫風險，停止精度細化。現有證據**不支持**啟用選擇性執行；正式完整測試保持不變。
