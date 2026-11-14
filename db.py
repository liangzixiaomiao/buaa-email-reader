# -*- coding: utf-8 -*-
"""
SQLite 本地数据库：存储邮件原文和分类结果。
数据库文件 emails.db 与本脚本放在同一目录。
"""

import os
import sqlite3
from contextlib import contextmanager

# 数据库文件路径（固定在本脚本所在目录，避免受运行时工作目录影响）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emails.db")


@contextmanager
def get_conn():
    """获取数据库连接（自动提交并关闭），row_factory 让结果可以按列名取值。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """建表（已存在则跳过）。"""
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS emails (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id  TEXT UNIQUE,   -- 邮件唯一标识，用于去重
                sender      TEXT,          -- 发件人
                subject     TEXT,          -- 主题
                body        TEXT,          -- 正文
                category    TEXT,          -- 分类：紧急待办/重要阅读/通知/垃圾
                summary     TEXT,          -- 摘要
                todo        TEXT,          -- 待办事项（可为空）
                mail_date   TEXT,          -- 邮件自带的时间
                created_at  TEXT DEFAULT (datetime('now', 'localtime'))  -- 入库时间
            )
            """
        )
        conn.commit()


def save_email(record):
    """保存一封邮件。message_id 重复时跳过（保留库里已有的分类，不覆盖）。

    返回 (是否新增, 记录 id)。
    """
    record = dict(record)
    record.setdefault("message_id", "")
    record.setdefault("mail_date", "")

    sql = """
        INSERT OR IGNORE INTO emails
            (message_id, sender, subject, body, category, summary, todo, mail_date)
        VALUES
            (:message_id, :sender, :subject, :body, :category, :summary, :todo, :mail_date)
    """
    with get_conn() as conn:
        cur = conn.execute(sql, record)
        conn.commit()
        if cur.rowcount > 0:
            return True, cur.lastrowid
        row = conn.execute(
            "SELECT id FROM emails WHERE message_id = ?", (record["message_id"],)
        ).fetchone()
        return False, (row["id"] if row else None)


def list_emails(categories=None, keyword=None, today_only=False):
    """按分类 / 关键字 / 是否今天入库来查询邮件，按 id 倒序返回。"""
    sql = "SELECT * FROM emails WHERE 1 = 1"
    params = []

    if categories:
        placeholders = ",".join("?" * len(categories))
        sql += f" AND category IN ({placeholders})"
        params.extend(categories)

    if keyword:
        sql += " AND (subject LIKE ? OR sender LIKE ? OR summary LIKE ? OR body LIKE ?)"
        like = f"%{keyword}%"
        params.extend([like] * 4)

    if today_only:
        sql += " AND date(created_at) = date('now', 'localtime')"

    sql += " ORDER BY id DESC"
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def update_email(email_id, **fields):
    """更新指定邮件，只更新传入的字段；值为 None 时会写入 NULL。"""
    allowed = ("category", "summary", "todo")
    pairs = [(k, v) for k, v in fields.items() if k in allowed]
    if not pairs:
        return 0

    assignments = ", ".join(f"{k} = ?" for k, _ in pairs)
    params = [v for _, v in pairs] + [email_id]
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE emails SET {assignments} WHERE id = ?", params)
        conn.commit()
        return cur.rowcount


def delete_emails(ids):
    """按 id 列表批量删除，返回删除条数。"""
    ids = list(ids)
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with get_conn() as conn:
        cur = conn.execute(f"DELETE FROM emails WHERE id IN ({placeholders})", ids)
        conn.commit()
        return cur.rowcount


def get_email(email_id):
    """按 id 查询单封邮件。"""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()


def count_by_category():
    """统计各分类的邮件数量，返回 dict。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS cnt FROM emails GROUP BY category"
        ).fetchall()
    return {row["category"]: row["cnt"] for row in rows}
