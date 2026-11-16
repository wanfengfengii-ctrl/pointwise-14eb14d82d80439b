# 正常 v1 业务契约

## 模式与值

模式是 JSON：基础类型字符串 null、int、long、string；有序模式数组表示联合。
具名 record 为 {"type":"record","name":"N","fields":[...]}；
每字段有 name、type，可选 aliases、default。具名 enum 有 type、name、
非空且唯一的 symbols，可选 aliases、default；enum default 必须属于 symbols。
record 和 enum 可有 aliases。具名类型名和字段名使用非空标识符。
字符串中非基础类型名引用同一文档中的具名定义，支持前向及相互递归引用。
具名定义唯一，字段名称/别名在同一 record 内不能重叠；别名不参与文档内引用查找。
未知属性、重复具名定义、悬空引用及不合法默认值报 SchemaError。

写入 record 值必须恰好提供所有写字段；不会用默认值补齐写入记录。
联合写入值为 {"branch": 分支编号, "value": 分支值}，
编号从 0 开始，仅在当前写入模式的当前联合内有意义。
显式 null 必须选择 null 分支。enum 值为符号字符串。
int 是有符号 32 位，long 是有符号 64 位；bool 不是整数。
string 是可编码为 UTF-8 的字符串，允许空串和内嵌零。

字段 default 是逻辑 JSON 值；若字段是联合，default 必须符合第一个分支，
不携带 branch 包装。record default 必须完整提供其字段；嵌套联合仍按第一分支解释。
有限记录及模式必须能由 Python 标准递归处理，不承诺超越解释器递归栈的深度。
不包含 map、array、外部模式引用及逻辑类型。

## 写读解析

先完整验证写入记录。写联合按值中 branch 选择一个分支，
未选择的分支不解析。读取联合按声明顺序选择第一个类型匹配分支；
嵌套读取联合按深度优先顺序选第一个匹配叶分支。
类型匹配后该分支值的解析失败即整条失败，不改选后面的分支。
基础类型须相同，额外允许 int 提升为 long；具名类型须种类相同且
写类型名称等于读取名称或属于读取类型 aliases。递归引用每次回到实际具名定义。
类型匹配只确定当前分支，record 的字段或 enum 的值仍可能解析失败。

读取字段先按同名写字段匹配，否则按读取 aliases 的声明顺序选择首个存在的写字段。
只有写入模式没有匹配字段时，才使用读取字段 default；没有 default 报 ResolutionError。
写字段存在时即使值是 null 也不使用默认值。
多余写字段不进入输出，但仍须通过完整写值校验。
enum 符号不存在于读取 enum 时，仅在读取 enum 有 default 时回退该符号。
结果是无 branch 包装的逻辑 JSON：record 为读取字段名的对象，联合为选中分支逻辑值。

## 公开入口

Lake(path=":memory:", failpoint=None) 可作上下文管理器；close() 关闭连接。
所有操作是单工作者同步调用，不允许调用者在公开操作外并行修改库。

- register_schema(name,schema) -> 整数 revision。每次成功登记新增不可变修订并更新名称 head；
  文件和任务只接受明确修订，不接受名称替代。catalog.head(name) 查询当前指向，
  catalog.get(revision) 返回独立 JSON 副本。无效登记不改变原 head。
- append(file_id,writer_revision,records) 接收完整有序列表，编码全部通过后
  原子保存文件、每行载荷和成功回执。返回 file_id、writer_revision、row_count、receipt。
  同 file_id、同修订及同记录重放返回原回执；同 ID 不同绑定/内容报 Conflict。
  文件身份不可替换。read_file(file_id) 返回上述字段及原始带联合标签的 records。
  重复行、空文件均合法。未知修订报 NotFound，坏值报 DataError。
- create_job(job_id,file_ids,reader_revision) 固定有序文件列表和读取修订，
  返回含 job_id、file_ids、reader_revision、status、error、receipt 的任务对象。
  输入列表可为空，可重复引用文件。创建成功初态 queued。
  同身份同绑定重放返回现有状态，不重新排队；不同绑定报 Conflict。
  未知文件/修订报 NotFound 且无任务。
- run_job(job_id) 按文件出现顺序、文件行顺序解析。成功返回 read_result 的结构；
  succeeded 重跑返回相同成果。遇 ResolutionError 原子发布 failed、error、
  失败回执及空结果，再抛出 ResolutionError；failed 重跑抛出同样错误。
  不发布前部候选行。DataError 或存储异常传播并回滚本次事务，保留原 queued 状态。
- read_result(job_id) 返回 job_id、status、reader_revision、rows、row_ids、error、receipt。
  rows 是完整逻辑记录列表；row_ids 中每项为 [file_id, 输入文件出现序号, 文件行序号]，
  两种序号都从 0 起，保持输入多重集身份。queued 的 rows/row_ids 为空，
  error/receipt 为 null；failed 的 rows/row_ids 为空且 error/receipt 非空；
  succeeded 的 error 为 null。未知任务报 NotFound。
- LakeError 是 SchemaError、DataError、ResolutionError、Conflict、NotFound 的共同基类。
  非空字符串身份校验失败报 DataError。SQLite/OSError 等非业务异常原样传播。

## 事务与持久关系

schema_revisions 保存不可变原文和摘要；schema_heads 只更新名称当前指向。
files 绑定写修订及稳定摘要/回执，file_rows 用 (file_id,ordinal) 保序。
jobs 固定读取修订和 JSON 输入清单；results 关联任务及来源出现坐标。
结果行、终态和完成回执必须同次提交。旧成功成果不随名称替换或新增文件改变。
SQLite 外键保持绑定；文件末行校验失败不产生文件或成功回执。

Database.transaction(label) 是可复用的原子设施：BEGIN IMMEDIATE，执行操作，
调用 checkpoint(label + ".before_commit")，然后 COMMIT；异常则 ROLLBACK。
Lake.db.failpoint 可设为接收 stage 字符串的函数；FailOnce(stage) 在指定边界
真正写入后、提交前抛一次 OSError。公开阶段为 initialize、catalog、archive、
job、publish，均附 .before_commit。故障注入不等于真实进程终止证据。

## 确定性回归

完整套件 tests/ 覆盖：字段别名、类型别名与反向不匹配、递归及相互递归、
两写修订相反联合顺序、两级链与 null 链尾、缺失默认值和显式 null、
int 提升、enum 默认及批末失败、重复文件/重复行坐标、
名称替换与固定任务、回执重放冲突、目录/文件/任务/发布事务失败及重试。
test_revision_binding.py 和 test_publication.py 的磁盘测试
在临时目录构造实际 SQLite 库，关闭重开检查绑定、结果、回执及提交前失败恢复。
