import os
import pymysql
import logging
from .config import load_config

logger = logging.getLogger(__name__)

class DatabaseManager:
    """
    TiDB 数据库管理类，负责连接池维护、跨库查询调度等。
    支持 source (源数据), relation (关系/图数据), insight (分析结果) 三个逻辑库。
    """
    def __init__(self):
        self.settings = load_config()
        self.connections = {}

    def _connect_raw(self, db_config, database=None):
        """建立原始 MySQL 连接"""
        ssl_config = None
        if str(db_config.get("ssl_mode", "")).upper() == "REQUIRED":
            ssl_ca = db_config.get("ssl_ca") or os.getenv("TIDB_SSL_CA") or "/etc/ssl/cert.pem"
            if os.path.exists(ssl_ca):
                ssl_config = {"ca": ssl_ca}
            else:
                logger.warning(
                    "TiDB SSL 模式设为 REQUIRED 但未找到 CA 证书；将尝试无证书连接。"
                )

        connect_kwargs = {
            "host": db_config["host"],
            "port": db_config["port"],
            "user": db_config["user"],
            "password": db_config["password"],
            "connect_timeout": db_config.get("connect_timeout", 5),
            "ssl": ssl_config,
            "charset": "utf8mb4",
            "cursorclass": pymysql.cursors.DictCursor,
            "autocommit": True,
        }
        if database:
            connect_kwargs["database"] = database
        return pymysql.connect(**connect_kwargs)

    def _ensure_database_exists(self, db_config):
        """确保目标数据库已创建（如果不存在则创建）"""
        bootstrap_db = db_config.get("bootstrap_db") or "sys"
        conn = self._connect_raw(db_config, database=bootstrap_db)
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{db_config['db_name']}`")
        finally:
            conn.close()

    def get_connection(self, db_type="source"):
        """
        根据业务类型获取数据库连接。
        db_type 可选值: 'source', 'relation', 'insight'
        """
        if db_type in self.connections:
            try:
                self.connections[db_type].ping(reconnect=True)
                return self.connections[db_type]
            except Exception:
                pass

        db_config = self.settings["database"][db_type]
        try:
            conn = self._connect_raw(db_config, database=db_config["db_name"])
            self.connections[db_type] = conn
            return conn
        except pymysql.err.OperationalError as exc:
            if exc.args and exc.args[0] == 1049: # 数据库不存在错误码
                logger.info(f"数据库 {db_config['db_name']} 不存在，正在自动创建...")
                self._ensure_database_exists(db_config)
                conn = self._connect_raw(db_config, database=db_config["db_name"])
                self.connections[db_type] = conn
                return conn
            raise
        except Exception as e:
            logger.error(f"连接到 {db_type} 数据库失败: {e}")
            raise

    def execute_query(self, query, params=None, db_type="source"):
        """执行 SQL 查询并返回所有结果"""
        conn = self.get_connection(db_type)
        with conn.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()

    def execute_batch(self, query, params_list, db_type="source"):
        """批量执行 SQL（如 INSERT INTO ... VALUES）"""
        conn = self.get_connection(db_type)
        with conn.cursor() as cursor:
            cursor.executemany(query, params_list)
            return cursor.rowcount

    def close_all(self):
        """关闭所有已建立的连接"""
        for conn in self.connections.values():
            conn.close()
        self.connections = {}

    def test_connection(self, db_type="source"):
        """简单连接测试"""
        conn = self.get_connection(db_type)
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 AS ok")
            return cursor.fetchone()

db_manager = DatabaseManager()
