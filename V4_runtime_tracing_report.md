# V4 Phase 4：執行時追蹤小型實驗

固定來源：SQLGlot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`；正式編號取自 `sample/tests.json`。本輪沒有查看答案檔、安裝套件、修改正式選測或容器入口。原始紀錄在 `V4_runtime_trace_results.json`、`V4_runtime_trace_probes.json`，無追蹤對照在 `V4_runtime_baseline_results.json`。

## 選取與實際執行

`runtime_trace_pilot.py` 依正式清單的檔案和登記耗時，從三個方言檔各取短測試；另依 `tests/test_optimizer.py` 中實際呼叫 `check_file()` 或 fixture loader 的方法，選四個 fixture 測試。這些選擇沒有使用樣本失敗答案。

| 類別 | 正式測試編號 | 執行結果 |
| --- | --- | --- |
| ClickHouse | 89, 92, 95, 96, 105, 112, 113, 116, 117, 126 | 10 通過 |
| SQLite | 828, 830, 834, 841, 847, 849, 852, 861, 867, 869 | 10 通過 |
| BigQuery | 19, 25, 29, 30, 33, 41, 45, 48 | 8 項匯入失敗：缺 `pytz` |
| Optimizer／SQL fixture | 1313, 1352, 1383, 1434 | 4 項匯入失敗：缺 `duckdb`；其後還依賴 Pandas |

共選 32 項，20 項真正執行且通過，12 項在測試方法前就因環境套件不足而停止。檔案內全部正式 ID 與每項狀態可在原始 JSON 核對。測試各用獨立 Python 3.13 子程序，容器關閉網路、主檔案系統唯讀，固定來源也唯讀。沒有為本次實驗安裝重型依賴。

## 匯入、方法與 I/O 證據

追蹤器分別記錄測試模組匯入期與 `unittest` 方法執行期的函式呼叫、實際行號、來源檔、模組載入及檔案讀取位置。

- 10 項 ClickHouse 測試在**匯入期**都載入 `sqlglot/generators/clickhouse.py`；其中 6 項在方法期呼叫其中程式。其測試模組直接匯入 `ClickHouse`。因此另外 4 項未在方法期呼叫 generator，仍有匯入失敗風險。
- 10 項 SQLite 測試在匯入期均**沒有**載入 `sqlglot/generators/sqlite.py`，但方法期均載入並呼叫該模組；這符合延遲方言分派。
- 20 項成功執行的測試在匯入共用 `Validator` 時，全部載入 BigQuery parser，方法期均未呼叫其方法。受控 `parse_one(..., read="bigquery")` 探針則實際呼叫 `BigQueryParser._parse_bracket` 與 `_parse_column`。BigQuery 的 8 項正式測試因 `pytz` 缺失而沒有方法期證據。
- 在受控探針中，單獨匯入共用 `Validator` 後，BigQuery 已載入、ClickHouse 和 SQLite 尚未載入；之後 `parse_one()` 對三個方言分別觸發對應 Parser／Generator。這重現 Phase 3 的載入差別，並提供方法級執行證據。
- 20 項成功的正式測試沒有觀測到 SQL fixture 讀取；四項選取的正式 optimizer 測試不能匯入。受控呼叫固定版 `tests.helpers.load_sql_fixture_pairs("optimizer/normalize.sql")`，確實讀到 `tests/fixtures/optimizer/normalize.sql`，呼叫位置是 `tests/helpers.py:45`，發生在執行期。匯入期沒有觀測到此 fixture 讀取。這是**探針**，不能冒充正式測試 ID 的 I/O 證據。

對照既有研究：fixture 讀取路徑與 Phase 1 的正向映射吻合；方言分派與 Phase 2 的來源關係吻合；ClickHouse 匯入期與 SQLite 延遲載入支持 Phase 3 區分匯入風險和方法風險。此小樣本沒有證實一條先前完全未知、可安全縮小候選的依賴。Phase 3 靜態圖對 ClickHouse generator／SQLite generator／BigQuery parser 分別有 1392／369／1474 個候選；本輪僅追蹤 20 項成功的正式測試，未觀測到的靜態候選全部保留。SQLite 的 51 條 Phase 2 動態新增關係也不能因本輪未覆蓋而刪除。

## 成本與限制

| 同一批 20 項可執行測試 | 子程序 CPU 合計 | 子程序牆鐘合計 |
| --- | ---: | ---: |
| 無追蹤 | 11,146.5 ms | 19,220.9 ms |
| 函式、行號與 I/O 追蹤 | 14,895.2 ms | 30,523.3 ms |

CPU 增加約 **33.6%**，牆鐘增加約 **58.8%**。32 項整批子程序 CPU 約 16,914 ms、牆鐘約 35,973 ms；單一子程序最高 RSS **57,468 KiB**。原始逐行結果 **3,691,739 bytes**，受控探針 **190,304 bytes**。若按 32 項平均直接外推到 1620 項，逐行檔約 187 MB；這只說明資料量級，不是可靠的全套成本估計。部分測試很重、需額外套件及可能啟動子程序；本輪沒有測量完整建置。20 項短測試每項隔離、串行追蹤已花約 30.5 秒牆鐘，不能據此保證在 10 分鐘 Docker 建置上限內完成全套。

追蹤器覆蓋 `builtins.open`、`io.open`、`pathlib.Path.open`、`os.open` 的成功讀取；不保證涵蓋 C 擴充、記憶體映射、子程序或尚未執行的動態路徑。它記錄讀取入口，不追蹤取得檔案描述符之後的所有 `read` 呼叫；`Path.open` 可能與 `io.open` 形成重複紀錄。每項測試都在獨立程序，匯入副作用與執行期分開，但只代表這個固定環境和這次輸入。缺少 `pytz`、`duckdb`、Pandas 使 BigQuery 與 optimizer 的正式測試覆蓋證據不完整。沒有觀測到某行、某個檔案或某次 I/O，均不構成排除其他測試的證明。

## 驗證與決定

Python 3.13 編譯通過；相關單元測試共 **125 項，123 通過、2 跳過、0 失敗**。初次以唯讀方式掛載研究目錄時，既有 `test_selector` 要在該目錄建立暫存資料而失敗；改用可寫研究目錄後全套通過，實驗執行仍保持唯讀。原型曾把開檔失敗也記為讀取；已修正為只在開檔成功後記錄，並加回歸測試。

**NO-GO：沒有證據支持排除任何正式測試或啟用選擇性執行。** 這個小型追蹤器適合驗證具體的匯入與分派關係，但在缺少必要測試依賴和全套覆蓋完整性證明的條件下，不適合產生可用於正式選測的覆蓋表。維持完整測試與原有回退。
