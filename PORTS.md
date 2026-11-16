# 离线编解码端口

均只依赖 Python 3.11 标准库，API 抛 SchemaError 或 DataError。
SDK1 与 SDK2 实现互不调用。二者的字节均编码一条完整记录，而非文件容器。

## SDK1：vendor/sdk1.py

encode(schema,value) -> bytes
decode(schema,payload) -> 带 branch 标签的 JSON 值
resolve(writer_schema,reader_schema,value) -> 无 branch 包装的逻辑值
Schema(schema) 执行内联模式校验；正常业务消费 resolve。

字节是 ASCII 头 IL1 加 LF，随后是 UTF-8 紧凑 JSON。
JSON 对象键排序，无空白，非 ASCII 字符直接 UTF-8 编码。
null/int/long/string 保持 JSON 原值；record 按模式字段顺序写成值数组；
enum 写成 symbols 中从零起的序号；联合写成 [分支序号, 分支 wire 值]。
引用按具名定义编码，字段名与类型名不重复写入载荷。解码器检查值和容器长度。
例如 samples.NODE 的 samples.CHAIN 是：

    IL1
    [1,[1,[2,[0,null]]]]

文件目录及写入修订由业务 SQLite 表保存，不嵌在每行字节中。

## SDK2：vendor/sdk2.py

validate(graph,root) -> None
encode(graph,root,value) -> bytes
decode(graph,root,payload) -> 带 branch 标签的 JSON 值

graph 是 {非空字符串节点ID: 节点对象}；root 是其中一个 ID。
所有边直接指向 ID，允许 record 经 union 回到自身或其他 record。
节点格式：

- {"kind":"null"}、{"kind":"int"}、{"kind":"long"}、{"kind":"string"}
- {"kind":"union","branches":[节点ID,...]}
- {"kind":"enum","name":"E","symbols":["a","b"],"aliases":["OldE"],"default":"a"}
- {"kind":"record","name":"N","aliases":["OldN"],"fields":[
  {"name":"value","target":"整数节点ID"},
  {"name":"next","target":"联合节点ID","aliases":["old_next"],"default":null}]}

aliases/default 可省略；record 的 default 是字段元数据，codec 不补字段，
不执行读取解析或校验字段 default 对应的读取语义。enum default 须属于 symbols。
具名定义唯一，字段名称/别名不能重叠，所有边闭合，不允许只有 union 的环。
ID 没有字段路径或联合序号含义。输入联合标签的 branch 属于该 union 节点。

字节头为 4 字节 4e 47 32 00；之后按根节点编码：
null 不占字节；int/long 分别为大端有符号 4/8 字节；
string 为大端无符号 4 字节 UTF-8 字节数，随后正文；
enum 为大端无符号 4 字节符号序号；
union 为大端无符号 4 字节分支序号，随后选中分支；
record 依字段顺序连接所有字段编码。截断、非法序号、无效 UTF-8、尾随字节拒绝。
tests/test_ports.py 有手写原生递归图、换序图、冻结字节和坏载荷对照。
端口不提供内联模式到图的转换，也不提供写入模式与读取模式间解析。
