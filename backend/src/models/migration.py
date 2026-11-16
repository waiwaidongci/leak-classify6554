from datetime import datetime,timezone
from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import String,JSON,DateTime,Index,text
from src.config.database import Base

# 方言感知的部分唯一索引（一个索引、两套方言谓词）：
# - PostgreSQL 与 SQLite 都只对 succeeded 行加唯一约束；
# - failed/importing 行允许同摘要重试，成功的同包导入在数据库层面被挡住，
#   并发导入同一封存包只落一份。
_partial_unique_index = Index(
 'uq_migration_package_succeeded','package_digest',unique=True,
 postgresql_where=text("status = 'succeeded'"),
 sqlite_where=text("status = 'succeeded'"))

class MigrationImport(Base):
 __tablename__='migration_imports'
 __table_args__=(_partial_unique_index,)
 id:Mapped[int]=mapped_column(primary_key=True)
 package_digest:Mapped[str]=mapped_column(String(64),index=True)
 status:Mapped[str]=mapped_column(String(20),default='importing')
 origin_station:Mapped[str]=mapped_column(String(120),default='')
 source_fingerprint:Mapped[str]=mapped_column(String(64),default='')
 summary:Mapped[dict]=mapped_column(JSON,default=dict)
 error:Mapped[str|None]=mapped_column(String(1000),nullable=True)
 actor:Mapped[str]=mapped_column(String(120),default='')
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
 finished_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
