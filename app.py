# -*- coding: utf-8 -*-
"""
Streamlit 可视化界面：拉取未读邮件 → 大模型分类 → 存入 SQLite → 查看/修改/删除 → 生成汇总。

启动方式：
    streamlit run app.py
"""

import os

import pandas as pd
import streamlit as st
from openai import OpenAI

import config
import db
import main as job

# 所有可选分类（与 main.py 中的提示词保持一致）
CATEGORIES = ["紧急待办", "重要阅读", "通知", "垃圾"]

st.set_page_config(page_title="邮件自动分类助手", page_icon="📬", layout="wide")
db.init_db()

# 当前生效的配置：优先真实环境变量，其次 .env
settings = config.get_settings()


def render_setup_form():
    """首次使用时的配置引导页：填完写入 .env，下次启动自动读取。"""
    st.title("📬 邮件自动分类助手")
    st.caption("首次使用：填写下面的配置并保存，会写入本地 .env（已被 .gitignore 忽略），下次启动自动记住。")

    with st.form("setup_form"):
        st.subheader("📮 邮箱设置")
        account = st.text_input("邮箱地址", placeholder="your_email@example.com")
        password = st.text_input(
            "邮箱授权码",
            type="password",
            help="不是登录密码：需在邮箱网页端开启 IMAP 服务后生成授权码",
        )
        col1, col2 = st.columns(2)
        server = col1.text_input("IMAP 服务器", value=settings["IMAP_SERVER"])
        port = col2.text_input("IMAP 端口", value=settings["IMAP_PORT"])

        st.subheader("🤖 大模型设置")
        base_url = st.text_input("base_url", placeholder="https://api.deepseek.com")
        api_key = st.text_input("api_key", type="password", placeholder="sk-...")
        model = st.text_input("模型名称", value=settings["LLM_MODEL"])

        submitted = st.form_submit_button("保存并进入", type="primary", width="stretch")

    if submitted:
        required = {
            "邮箱地址": account,
            "授权码": password,
            "base_url": base_url,
            "api_key": api_key,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            st.error("以下必填项还没填：" + "、".join(missing))
        else:
            config.save_env(
                {
                    "EMAIL_ACCOUNT": account.strip(),
                    "EMAIL_PASSWORD": password.strip(),
                    "IMAP_SERVER": server.strip() or "mail.buaa.edu.cn",
                    "IMAP_PORT": port.strip() or "993",
                    "LLM_BASE_URL": base_url.strip(),
                    "LLM_API_KEY": api_key.strip(),
                    "LLM_MODEL": model.strip() or "deepseek-chat",
                }
            )
            st.success("配置已保存到 .env，正在进入…")
            st.rerun()


# 首次使用（没有 .env 或关键项为空）先显示配置引导页
if config.is_first_run():
    render_setup_form()
    st.stop()

# ---------------------- 侧边栏：连接设置与操作按钮 ----------------------

with st.sidebar:
    st.subheader("📮 邮箱设置")
    account = st.text_input("邮箱地址", value=settings["EMAIL_ACCOUNT"])
    password = st.text_input("授权码", value=settings["EMAIL_PASSWORD"], type="password")
    st.caption(f"IMAP 服务器：{settings['IMAP_SERVER']}:{settings['IMAP_PORT']}")

    st.subheader("🤖 大模型设置")
    base_url = st.text_input("base_url", value=settings["LLM_BASE_URL"])
    api_key = st.text_input("api_key", value=settings["LLM_API_KEY"], type="password")
    model = st.text_input("模型名称", value=settings["LLM_MODEL"])
    # 界面里改的模型名同步回 main.py 的模块变量，供 classify_email 使用
    job.LLM_MODEL = model

    # 把侧边栏当前填写的值写回 .env，下次启动直接沿用
    if st.button("💾 保存当前设置为默认", width="stretch"):
        config.save_env(
            {
                "EMAIL_ACCOUNT": account.strip(),
                "EMAIL_PASSWORD": password.strip(),
                "LLM_BASE_URL": base_url.strip(),
                "LLM_API_KEY": api_key.strip(),
                "LLM_MODEL": model.strip(),
            }
        )
        st.success("已保存到 .env")

    st.divider()
    run_clicked = st.button("🚀 拉取未读邮件并分类", type="primary", width="stretch")


# ---------------------- 顶部：分类统计 ----------------------


def render_stats():
    """用四个指标卡展示数据库中各分类的邮件数量。"""
    counts = db.count_by_category()
    cols = st.columns(5)
    total = sum(counts.values())
    cols[0].metric("总计", total)
    for col, name in zip(cols[1:], CATEGORIES):
        col.metric(name, counts.get(name, 0))


st.title("📬 邮件自动分类助手")
render_stats()

# ---------------------- 点击「拉取并分类」后的处理 ----------------------

if run_clicked:
    if not password:
        st.sidebar.error("请先填写邮箱授权码")
    elif not base_url:
        st.sidebar.error("请先填写 base_url")
    elif not api_key:
        st.sidebar.error("请先填写 api_key")
    else:
        progress = st.progress(0.0)
        status = st.empty()
        try:
            status.info("正在连接邮箱、读取未读邮件…")
            try:
                imap_port = int(settings["IMAP_PORT"])
            except (TypeError, ValueError):
                imap_port = 993
            emails = job.fetch_unread_emails(
                account=account,
                password=password,
                server=settings["IMAP_SERVER"],
                port=imap_port,
            )

            if not emails:
                status.warning("没有未读邮件")
            else:
                client = OpenAI(base_url=base_url or None, api_key=api_key)

                def on_progress(idx, total, item):
                    # 每封邮件刷新一次进度条和状态文字
                    progress.progress(idx / total)
                    status.info(f"正在分类 {idx}/{total}：{item['subject']}")

                new_count, skip_count = job.process_emails(emails, client, on_progress)
                progress.progress(1.0)
                status.success(
                    f"完成：读取 {len(emails)} 封，新增 {new_count} 封，重复跳过 {skip_count} 封"
                )
                # 重新生成当天的 Markdown 汇总
                rows = db.list_emails(categories=job.KEY_CATEGORIES, today_only=True)
                job.write_summary_md(rows)
        except Exception as exc:
            st.error(f"执行失败：{exc}")

# ---------------------- 主体：两个标签页 ----------------------

tab_list, tab_summary = st.tabs(["📥 邮件管理", "📄 每日汇总"])

# ============ 标签页 1：邮件管理（查看 / 修改 / 删除） ============

with tab_list:
    f_col1, f_col2 = st.columns([2, 3])
    with f_col1:
        selected_categories = st.multiselect("按分类筛选", CATEGORIES, default=CATEGORIES)
    with f_col2:
        keyword = st.text_input("搜索（主题 / 发件人 / 摘要 / 正文）", placeholder="输入关键字后自动过滤")

    rows = db.list_emails(categories=selected_categories or None, keyword=keyword or None)

    if not rows:
        st.info("数据库里还没有符合条件的邮件，先点左侧「拉取未读邮件并分类」。")
    else:
        # 转成 DataFrame 供表格编辑；列名用中文便于阅读
        df = pd.DataFrame([dict(r) for r in rows])
        df = df.rename(
            columns={
                "id": "ID",
                "category": "分类",
                "summary": "摘要",
                "todo": "待办事项",
                "subject": "主题",
                "sender": "发件人",
                "created_at": "入库时间",
            }
        )
        df = df[["ID", "分类", "摘要", "待办事项", "主题", "发件人", "入库时间"]]
        df.insert(0, "选择", False)  # 勾选列，用于批量删除
        original = df.copy()  # 保存原始值，用于判断哪些行被改过

        st.caption("「分类」「摘要」「待办事项」可直接双击修改，勾选最左侧方框可删除。")

        edited = st.data_editor(
            df,
            key=f"email_editor_{'|'.join(selected_categories)}_{keyword}",
            hide_index=True,
            width="stretch",
            disabled=["ID", "主题", "发件人", "入库时间"],
            column_config={
                "选择": st.column_config.CheckboxColumn("删除", width="small"),
                "ID": st.column_config.NumberColumn("ID", width="small"),
                "分类": st.column_config.SelectboxColumn("分类", options=CATEGORIES, required=True),
                "摘要": st.column_config.TextColumn("摘要", width="large"),
                "待办事项": st.column_config.TextColumn("待办事项", width="large"),
                "主题": st.column_config.TextColumn("主题", width="large"),
            },
        )

        btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 3])
        save_clicked = btn_col1.button("💾 保存修改", type="primary", width="stretch")
        delete_clicked = btn_col2.button("🗑️ 删除选中", width="stretch")

        # ---- 保存修改：逐行对比，只更新有变化的字段 ----
        if save_clicked:
            # 表头中文名 -> 数据库字段名
            column_map = {"分类": "category", "摘要": "summary", "待办事项": "todo"}
            changed = 0
            for i in range(len(edited)):
                row_id = int(edited.at[i, "ID"])
                updates = {}
                for column, field in column_map.items():
                    before = original.at[i, column]
                    after = edited.at[i, column]
                    before = "" if pd.isna(before) else str(before)
                    after = "" if pd.isna(after) else str(after)
                    if after != before:
                        # 空字符串写回数据库时存为 NULL
                        updates[field] = after.strip() or None
                if updates:
                    db.update_email(row_id, **updates)
                    changed += 1
            if changed:
                st.success(f"已保存 {changed} 封邮件的修改")
            else:
                st.info("没有检测到修改")

        # ---- 删除选中 ----
        if delete_clicked:
            ids = [int(v) for v in edited.loc[edited["选择"] == True, "ID"].tolist()]
            if not ids:
                st.warning("请先勾选要删除的邮件")
            else:
                deleted = db.delete_emails(ids)
                st.success(f"已删除 {deleted} 封邮件")
                st.rerun()

        # ---- 邮件详情：选择一封查看完整正文 ----
        with st.expander("🔍 查看邮件详情"):
            options = {f"{r['id']} | {r['subject']}": r["id"] for r in rows}
            picked = st.selectbox("选择邮件", list(options.keys()))
            if picked:
                detail = db.get_email(options[picked])
                st.write(f"**发件人**：{detail['sender']}")
                st.write(f"**主题**：{detail['subject']}")
                st.write(f"**分类**：{detail['category']}")
                st.write(f"**摘要**：{detail['summary']}")
                st.write(f"**待办事项**：{detail['todo'] or '无'}")
                st.text_area("正文", detail["body"] or "", height=240, disabled=True)

# ============ 标签页 2：每日汇总 ============

with tab_summary:
    col_a, col_b = st.columns([1, 3])
    if col_a.button("📝 重新生成 每日汇总.md", width="stretch"):
        rows = db.list_emails(categories=job.KEY_CATEGORIES, today_only=True)
        job.write_summary_md(rows)
        st.success("已重新生成")

    summary_rows = db.list_emails(categories=job.KEY_CATEGORIES, today_only=True)
    markdown = job.build_markdown(summary_rows)
    st.download_button(
        "⬇️ 下载 Markdown",
        data=markdown,
        file_name="每日汇总.md",
        mime="text/markdown",
        width="content",
    )
    st.caption(f"汇总范围：今天入库的「紧急待办」和「重要阅读」，共 {len(summary_rows)} 封")
    st.markdown(markdown)

    if os.path.exists(job.OUTPUT_FILE):
        st.caption(f"本地文件：{job.OUTPUT_FILE}")
