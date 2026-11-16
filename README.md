# Revision Lake

Python 3.11 标准库实现的模式化数据湖归档组件，无网络依赖。
来源文件按不可变写入修订归档；加工任务固定输入清单和读取修订，
把不同历史模式的数据解析为同一个逻辑读取模式并原子发布。

## 运行

在项目根目录执行：

    python3.11 -B -m unittest discover -s tests -t . -v
    python3.11 -B -m lake.demo

demo 使用内存 SQLite，展示历史加工、同名模式替换、固定任务、
提交前异常回滚及重试、enum 不兼容整批失败。磁盘重开回归使用测试临时目录。
创建可携带的完整旧库：

    python3.11 -B -m lake.samples ./sample-v1.sqlite

构库器要求空库，包含全部模式修订、被替换的名称 head、历史文件、
重复行、有序重复文件输入、成功成果及回执、queued 任务和 failed 任务。
有限数据及独立手算逻辑预期见 lake/samples.py。

## 使用

    from lake import Lake

    schema = {"type": "record", "name": "Reading",
              "fields": [{"name": "id", "type": "int"}]}
    with Lake("archive.sqlite") as lake:
        revision = lake.register_schema("source", schema)
        lake.append("day-1", revision, [{"id": 2}, {"id": 2}])
        lake.create_job("daily", ["day-1"], revision)
        result = lake.run_job("daily")
        assert result["rows"] == [{"id": 2}, {"id": 2}]

完整格式、公开接口及错误语义见 CONTRACT.md；离线编解码格式见 PORTS.md。

目录由 lake/schema_catalog.py 持有，文件由 lake/file_archive.py 持有，
任务绑定由 lake/job_registry.py 持有。lake/materializer.py 通过 SDK1
完成写读解析，lake/result_store.py 在同一事务发布成果与终态。
lake/db.py 提供真实 SQLite 事务及提交前故障点。

正常归档格式是 storage_version=1，使用 vendor/sdk1.py。
vendor/sdk2.py 是独立的节点图编解码端口；它没有接入正常业务存储，
没有内联模式转换器或写入模式到读取模式的解析器。
项目支持维护窗口内单工作者，不提供并发调度、外部模式引用或完整 Avro 标准。
