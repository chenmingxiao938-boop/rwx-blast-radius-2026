# V4 Phase 1：SQL fixture 與測試對應研究

固定來源：sqlglot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`。只讀取 `sample/commits/*.patch` 與 `sample/tests.json`；未讀取答案檔，未修改正式選測、Docker 或排程。

## 載入方式

`tests/helpers.py:4,34-48` 以自身所在目錄定位 `tests/fixtures`，`load_sql_fixtures` 與 `load_sql_fixture_pairs` 在產生器開始迭代時開檔。`tests/test_optimizer.py:221-228` 的 `check_file(file, ...)` 把字面參數組成 `optimizer/{file}.sql`；多個 SQL 案例經 `subTest` 在同一個註冊測試 ID 內執行。`tests/test_optimizer.py:2078,3668,3956` 另有直接呼叫。`tests/test_serde.py:24` 與 `tests/test_transpile.py:1050` 共同讀取 `identity.sql`，證明 fixture 可供多個註冊測試共用。`tests/test_executor.py:118-119` 在 `setUpClass` 讀取 TPC-H/TPC-DS fixture；`tests/test_jsonpath.py:69` 另以 `open()` 讀 JSON fixture。掃描到的 SQL loader 呼叫均在測試執行／類別 setup 階段，沒有發現由 SQL fixture 內容產生測試 ID 的收集邏輯；仍不能據此證明所有收集插件或間接讀取不存在。

原型 `map_fixture_impacts(changed_paths, test_metadata, source_root)` 回傳 `FixtureMappingResult(candidate_ids, candidate_numbers, file_impacts, unresolved, fallback_required)`。每個 `FixtureImpact` 包含路徑、候選、原始測試檔行號、載入方法與未解決原因。正式編號直接取自 `tests.json`。此映射只提供正面讀取證據，`fallback_required` 始終為 `True`。

## 八個純 fixture 補丁

下表路徑均以 `tests/fixtures/optimizer/` 為前綴；測試 ID 均以 `tests/test_optimizer.py::TestOptimizer::` 為前綴。候選數每例為 1，八例全部仍須全套回退。

| 補丁 | SQL 檔名 | 已證實讀取測試 | 正式編號 | 呼叫點 |
| --- | --- | --- | ---: | --- |
| 2vdj | pushdown_predicates.sql | test_pushdown_predicates | 1377 | test_optimizer.py:2585 |
| 6vvp | qualify_columns__invalid.sql | test_qualify_columns__invalid | 1383 | test_optimizer.py:2078 |
| egys | simplify.sql | test_simplify | 1434 | test_optimizer.py:2160 |
| f4bn | pushdown_cte_alias_columns.sql | test_pushdown_cte_alias_columns | 1375 | test_optimizer.py:2072 |
| gbhk | annotate_types.sql | test_annotate_types | 1302 | test_optimizer.py:3668 |
| mthh | merge_subqueries.sql | test_merge_subqueries | 1343 | test_optimizer.py:2929 |
| nwnx | pushdown_projections.sql | test_pushdown_projection | 1378 | test_optimizer.py:2157 |
| st8f | isolate_table_selects.sql | test_isolate_table_selects | 1335 | test_optimizer.py:338 |

所有八條關係由固定版測試原始碼的 AST 呼叫點支持；`check_file` 的實際路徑組法另由其函式本體支持。受控執行固定版 `tests/helpers.py` 並攔截 `open()`，確認樣本涉及的 10 個 SQL fixture（含兩個混合補丁中的 fixture）均由 helper 開啟。此觀察沒有執行每個完整測試，也沒有證明不存在其他讀取者。

## 風險與成本

現有原型只辨識註冊類別方法內的兩個 helper 名稱，以及已驗證的 `TestOptimizer.check_file` 呼叫；其他自訂 `open()`、別名、模組層測試、跨方法間接呼叫和資料驅動路徑未完整建模。缺失檔、非 SQL 檔、非標準路徑與未找到註冊讀取者會留下明確未解決原因。已找到讀取者仍保留「其他直接或間接讀取未排除」；所以八例都**不足以支持排除其他測試**。

Python 3.13 容器中的 88 項相關測試：87 通過、1 跳過、0 失敗。第一次唯讀掛載使既有 scheduler 測試無法建立暫存目錄；改用可清理掛載重跑後通過。新增的類別 setup 測試也確認共用讀取會影響該類別全部註冊方法。批次共用一次 AST 掃描時，八例分析 CPU 327.35 ms、程序峰值 RSS 36,556 KiB；逐例重掃約 2,422.36 ms CPU、38,832 KiB 峰值。這是離線原型測量，若放進正式執行，應先於建置期預產映射以避免每批重掃。

結論：可把此模組作為 Shadow Mode 的**正面候選診斷**，尚不能用於正式縮減測試。下一階段應先追蹤完整測試執行期間的檔案讀取，核對收集與類別 setup，然後處理未建模的自訂載入與間接消費；在證據閉合前保留全套回退。

重現：在 Python 3.13 環境執行 `python -m unittest test_fixture_impact_map`，再執行 `python audit_fixture_impact.py <sample目錄> <固定sqlglot目錄>` 與 `python observe_fixture_loader.py <sample目錄> <固定sqlglot目錄>`。這些命令不讀取答案檔。
