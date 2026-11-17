# 投影导出交付说明

本文件描述新增的有限状态投影导出（`PROJECTION.md`）的实现位置、不变量、
核账方式和交付步骤。原 v1 契约、历史数据、tests/ 原文件未改动。

## 实现位置

- `lake/projection.py`
  - `tagged_resolve` / `strip_tags`：按固定写修订与固定读取修订重放原 SDK1
    写读解析，并保留读取联合每一层的分支编号路径：不复制写模式编号，
    不按无标签成果猜分支。`strip_tags` 只用于核对固定成果与导出逻辑记录。
  - `compile_selection`：完整校验 selection，返回按状态绑定的规则。
  - `SchemaBuilder`：只从输出根构造可达定义，按 (源 record, 状态) 生成
    互不混同的专化记录；联合层次、分支数、顺序与 enum 符号保持不变，
    不复制 aliases/default。
  - `Projector`：把带标签的读取成果转换为输出模式的带标签记录。
  - `ProjectionStore`：导出绑定、发布与读取。
- `lake/db.py`：新增 `projection_exports`、`projection_rows` 两张表。
  模式修订写入既有 `schema_revisions`，载荷写入既有 `files`/`file_rows`，
  但**不触碰** `schema_heads`。
- `lake/__init__.py`：公开 `Lake.export_projection` 与 `Lake.read_projection`。

## 绑定与不变量

- 源固定为任务当时的 `jobs.reader_revision`、`results` 与输入清单，
  与当前名称 head 无关。
- 导出身份 = 对 `job_id` 与规范化（键排序）selection 的摘要；
  重放返回原 `export_id`/`file_id`/`writer_revision`/`receipt`，不新增资源。
- 文件名优先使用确定性 `pfile-...`；若调用者已占用该文件名，
  使用带后缀的替代名，绝不覆盖调用者文件。
- 合成模式以 `projection:<export_id>` 名称登记为普通不可变修订，
  不创建或移动名称 head。
- `read_projection` 返回六项绑定加 `schema`（目录副本）、无联合包装的
  `rows`、与源 `row_ids` 完全相同的 `row_ids`。
- `read_file(file_id)` 可独立按导出修订解码带联合标签的记录；
  其文件回执与导出回执可以不同。
- 一次事务发布模式修订、全部文件行、导出绑定、来源坐标与回执；
  `publish.before_commit` 在任何写入之后、提交之前触发，失败整批回滚。

## 核账方式

`tests/test_projection.py` 为独立核账，不复用投影内部助手来构造期望值：

- 手写的带标签期望（换序联合、嵌套联合、默认值与显式 null、enum 回退）。
- 通过 `read_file` 观察 `{"branch": n, "value": ...}`，与手算对照。
- 用导出模式对每条记录做 `encode`/`decode` 往返并用 `sdk1.resolve(导出, 导出)`
  核对无标签行。
- 同一 record 的不同状态形状不得共享具名定义；selection 键序不影响身份。
- 空文件/空任务仍生成完整递归闭合模式。
- 非法选择、queued/failed 任务、非 record 根、未知身份的错误类别。
- 内存回滚重试、磁盘关闭重开、重放不新增资源、调用者文件不被覆盖。
- `tests/test_projection_examples.py` 在统一回归中执行
  `projection_examples.verify()`，覆盖公开手算样例的整链路。

## 运行与演示

    python3.11 -B -m unittest discover -s tests -t . -v
    python3.11 -B -m lake.demo
    python3.11 -B projection_examples.py fixtures
    python3.11 -B projection_examples.py verify

`.pointwise-runtime.json` 仍为 version=1、kind=cli、setup 为准备命令列表；
verify 运行原回归加新增核账，demo 使用临时磁盘库展示递归投影导出，
关闭连接后重开读取同一导出并核对重放身份。
