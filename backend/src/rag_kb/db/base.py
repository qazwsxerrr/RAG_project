"""src/rag_kb/db/base.py —— ORM 声明基类 Base。

所有 ORM 模型都继承它，Base.metadata 汇总全部表定义，供 session.py 的 create_all 建表；
单独放在小文件里是为了避免 models 与 session 之间循环导入。
"""

from sqlalchemy.orm import declarative_base

Base = declarative_base()
