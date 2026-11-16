# 固定成果的有限状态投影导出

这是在现有 v1 归档上新增的能力。普通接口、历史数据及 tests/ 原文件保持兼容；不迁移 SDK2。
已有实现没有本文件描述的导出入口。公开样例 projection_examples.py 只提供输入、手算结果和调用核验，不提供投影实现。

## 入口和绑定

新增 Lake.export_projection(job_id, selection) 和 Lake.read_projection(export_id)。
仅导出 succeeded 任务；源读取模式的根必须是 record（定义或引用）。
queued/failed 任务或非 record 根报 DataError，不自动运行任务；未知身份报 NotFound。
历史上已经完成、没有联合标签的成果也须支持。
导出固定任务当时的 reader_revision、成果、来源出现坐标和 selection，不能采用当前名称 head。
允许从固定原文件重算、全量复制、重扫和完整暂存；不要求增量执行、限内存或性能优化。

export_projection 返回恰含 export_id、job_id、file_id、writer_revision、row_count、receipt 的字典。
export_id/file_id/receipt 均为非空字符串；writer_revision 是新归档实际绑定的目录修订。
export_id 由实现生成。同一 job_id 和结构相同的 selection 返回同一导出身份和原回执，不新增修订或文件。
对象键的排列不影响 selection 身份；状态名称属于规则身份，不要求识别改名后的等价规则。
file_id 指向正常 read_file 可读取的不可变 SDK1 归档，不能只保存投影逻辑 JSON。
不得覆盖调用者已有文件、修订或 schema_heads；合成模式可以保存到目录但不改变既有名称 head。

read_projection 返回上述六项及 schema、rows、row_ids。
schema 等于 catalog.get(writer_revision)，rows 是去除联合包装的投影逻辑记录；
read_file(file_id) 则返回按该模式解码的带联合包装 records，其 receipt 与导出 receipt 可不同。
row_ids 与源 read_result 的 row_ids 完全相同，按输入文件出现顺序和文件行序保留重复。
所有输入在调用时取副本；返回值修改、后续目录登记及关闭重开均不得改变已发布导出。

## selection 的完整语法

selection 是恰含 root、states 的 JSON 对象。root 是初始状态名。
states 是非空对象，键为非空标识符状态名；每个值恰含 record、fields。
record 是源固定读取模式中某个 record 的正式名称，不接受 aliases。
fields 是对象：键为该 record 要保留的字段正式名称，未列出的字段删除。
字段值是对象，指定沿此字段进入下一条 record 时的状态，形式为 {记录正式名: 状态名}。
求这个字段类型的 record 叶：展开具名引用和任意嵌套联合，遇到 record 即停止，不进入其字段。
转移表必须恰好覆盖所有这样的 record 叶；同名叶只列一次，目标状态的 record 必须等于该叶正式名。
基础类型和 enum 不消耗状态，也没有转移；无 record 叶的保留字段写 {}。
联合每一层均保持当前字段的转移表，直到进入 record 才切换状态。
root 必须存在且绑定源根 record；全部状态（包括不可达状态）均须满足上述规则。
未知属性、未知字段/类型/状态、缺失或多余转移、错绑 record、非法容器或名称均报 SchemaError。
空 fields 合法。fields 的对象键序不控制输出：输出字段依源读取模式的字段声明顺序。

例：Node(value:int,next:[null,Node]) 的选择为：
    {"root":"even","states":{
      "even":{"record":"Node","fields":{"value":{},"next":{"Node":"odd"}}},
      "odd":{"record":"Node","fields":{"next":{"Node":"even"}}}}}
三层逻辑链 value=1、2、3 导出为
    {"value":1,"next":{"next":{"value":3,"next":null}}}
这不是深度限制；该规则对任意可由原 SDK 处理的有限链均成立。
支持前向及相互递归、空文件和空输入任务；没有行时也必须生成完整闭合模式。
输出模式须保留规则涉及的全部联合分支和递归闭包，不能只按本次出现的值或样本深度建模式。

## 模式和记录必须一致

源模式按固定读取修订解释。不同选择状态下同一 record 可有不同字段形状。
输出仍是 CONTRACT.md 的内联 SDK1 模式；合成类型名由实现选择，引用必须闭合，具名定义唯一。
只从输出根构造可达定义；保留字段原名及声明次序，基础类型、enum 符号及次序不变。
输出不携带 record/enum/字段的 aliases 或 default；默认值已在源读取成果中具体化。
具名类型可重命名，结构等价的专化允许共享，但不能将不等价形状混同。
字段删除只改变 record 形状；联合层次、分支数、声明顺序均保持源读取模式，不能合并或删掉同形分支。

每次出现所用的联合分支必须是原固定写读解析实际选择的读取分支，包括嵌套联合的整条编号路径。
不能复制写模式编号，不能按投影后值或无标签成果重新猜分支。
缺失字段的 default 仍按原 SDK 的默认值及首匹配读取语义确定分支；显式 null 不得变成缺省。
例如写 item:[A,B]、读 item:[B,A]，A/B 都是 {v:int}，
两行分别写 A(v=7)、B(v=7)，源逻辑成果相同；即使两类都删净字段，
导出 item 也必须分别为 {"branch":1,"value":{}}、{"branch":0,"value":{}}。
该规则由 read_file 的带标签记录观察，不能仅比较无标签 rows。
不得改变源任务的公开成果、状态或回执，也不得要求先重新运行成功任务。

## 原子性与异常

投影模式修订、新文件全部载荷、导出绑定、来源坐标和成功回执同次发布。
非法选择、记录转换/编码失败或存储异常不得留下修订、文件、导出或回执，也不得改变旧成果及既有 head。
非法选择报 SchemaError，编码错误沿用 DataError；非业务异常原样传播。
导出最终提交复用 publish.before_commit 故障阶段，FailOnce 在真正写入后、提交前触发。
失败后相同请求可重试；成功重放不能新建资源。关闭重开后导出仍可按目录模式独立解码。
本题只有单工作者同步导出，不涉及进程杀死、并发、回收、外部模式、数组或 map。

## 已提供入口和交付核验

    python3.11 -B projection_examples.py fixtures
核对样例源模式、写入值、固定成果和手算预期，适用于初始基线。
    python3.11 -B projection_examples.py verify
功能完成后贯穿三层选择、换序联合、默认值与 null、重复来源、提交失败重试以及磁盘重开；不是完整验收的替代。

保留完整原测试，补充独立的模式/带标签记录核账及交接说明。
沿已有 .pointwise-runtime.json 接通联合 verify，实际执行原回归和新增核验；
demo 展示一个递归投影导出并关闭重开读取的真实结果，不能只打印成功。
清单仍使用当前 version、kind 和 argv 结构，setup 继续是准备命令列表。
