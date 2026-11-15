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

# 可选值统一定义在 main.py，避免界面和提示词不一致
CATEGORIES = list(job.CATEGORIES)
EMAIL_TYPES = list(job.EMAIL_TYPES)

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


def render_stats(rows):
    """指标卡：总数 + 四档紧急程度 + 已超时。"""
    counts = db.count_by_category()
    overdue_count = sum(1 for r in rows if db.is_overdue(r.get("deadline")))
    cols = st.columns(6)
    cols[0].metric("总计", sum(counts.values()))
    for col, name in zip(cols[1:5], CATEGORIES):
        col.metric(name, counts.get(name, 0))
    cols[5].metric("⏰ 已超时", overdue_count)


all_rows = [dict(r) for r in db.list_emails()]

st.title("📬 邮件自动分类助手")
render_stats(all_rows)

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
                # 重新生成 Markdown 汇总（含历史遗留的已超时邮件）
                rows = db.list_emails(categories=job.KEY_CATEGORIES)
                job.write_summary_md(rows)
        except Exception as exc:
            st.error(f"执行失败：{exc}")


# ---------------------- 邮件表格 + 预览面板 ----------------------


def render_panel(rows, key_prefix):
    """左侧可编辑表格，右侧快速预览，两者共用同一批邮件。"""
    if not rows:
        st.info("没有符合条件的邮件。")
        return

    df = pd.DataFrame([dict(r) for r in rows])
    df["状态"] = df["deadline"].apply(lambda d: "已超时" if db.is_overdue(d) else "")
    df = df.rename(
        columns={
            "id": "ID",
            "college": "学院",
            "email_type": "类型",
            "category": "紧急程度",
            "deadline": "截止时间",
            "summary": "摘要",
            "todo": "待办事项",
            "subject": "主题",
            "sender": "发件人",
            "created_at": "入库时间",
        }
    )
    df = df[
        ["ID", "状态", "学院", "类型", "紧急程度", "截止时间",
         "摘要", "待办事项", "主题", "发件人", "入库时间"]
    ]
    df.insert(0, "选择", False)  # 勾选列，用于批量删除
    original = df.copy()  # 保存原始值，用于判断哪些行被改过

    left, right = st.columns([2.2, 1], gap="medium")

    with left:
        st.caption("「学院」「类型」「紧急程度」「截止时间」「摘要」「待办事项」可直接双击修改，勾选最左侧方框可删除。")
        edited = st.data_editor(
            df,
            key=f"editor_{key_prefix}",
            hide_index=True,
            width="stretch",
            disabled=["ID", "状态", "主题", "发件人", "入库时间"],
            column_config={
                "选择": st.column_config.CheckboxColumn("删除", width="small"),
                "ID": st.column_config.NumberColumn("ID", width="small"),
                "状态": st.column_config.TextColumn("状态", width="small"),
                "学院": st.column_config.TextColumn("学院", width="medium"),
                "类型": st.column_config.SelectboxColumn("类型", options=EMAIL_TYPES, required=True),
                "紧急程度": st.column_config.SelectboxColumn("紧急程度", options=CATEGORIES, required=True),
                "截止时间": st.column_config.TextColumn(
                    "截止时间", width="small", help="格式 YYYY-MM-DD HH:MM，留空表示没有截止时间"
                ),
                "摘要": st.column_config.TextColumn("摘要", width="large"),
                "待办事项": st.column_config.TextColumn("待办事项", width="large"),
                "主题": st.column_config.TextColumn("主题", width="large"),
            },
        )

        btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 3])
        save_clicked = btn_col1.button(
            "💾 保存修改", type="primary", width="stretch", key=f"save_{key_prefix}"
        )
        delete_clicked = btn_col2.button(
            "🗑️ 删除选中", width="stretch", key=f"delete_{key_prefix}"
        )

        # ---- 保存修改：逐行对比，只更新有变化的字段 ----
        if save_clicked:
            # 表头中文名 -> 数据库字段名
            column_map = {
                "学院": "college",
                "类型": "email_type",
                "紧急程度": "category",
                "截止时间": "deadline",
                "摘要": "summary",
                "待办事项": "todo",
            }
            changed = 0
            for i in range(len(edited)):
                row_id = int(edited.at[i, "ID"])
                updates = {}
                for column, field in column_map.items():
                    before = original.at[i, column]
                    after = edited.at[i, column]
                    before = "" if pd.isna(before) else str(before)
                    after = "" if pd.isna(after) else str(after)
                    if after == before:
                        continue
                    if field == "deadline":
                        # 统一成 YYYY-MM-DD HH:MM，非法输入写回空值
                        updates[field] = job.normalize_deadline(after) or None
                    else:
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

    # ---- 右侧：快速预览 ----
    with right:
        st.subheader("📄 快速预览")
        options = {f"{r['id']} | {r['subject'][:36]}": r["id"] for r in rows}
        picked = st.selectbox("选择邮件", list(options.keys()), key=f"preview_{key_prefix}")
        detail = dict(db.get_email(options[picked]))

        st.markdown(f"**{detail['subject']}**")
        st.caption(f"{detail['sender']} · 入库 {detail['created_at']}")
        meta = (
            f"🏫 {detail.get('college') or '其他'}　"
            f"📌 {detail.get('email_type') or '其他'}　"
            f"⚡ {detail['category']}"
        )
        if detail.get("deadline"):
            meta += f"　⏰ {detail['deadline']}"
        st.markdown(meta)

        if db.is_overdue(detail.get("deadline")):
            st.error("已超时：该邮件的截止时间已过")
        else:
            st.success("未超时")

        st.markdown(f"**摘要**：{detail['summary'] or '无'}")
        st.markdown(f"**待办**：{detail['todo'] or '无'}")
        st.caption("正文预览（前 600 字）")
        st.text_area(
            "正文",
            (detail["body"] or "")[:600],
            height=280,
            disabled=True,
            label_visibility="collapsed",
            key=f"body_{key_prefix}",
        )


# ---------------------- 主体：三个标签页 ----------------------

tab_list, tab_overdue, tab_summary = st.tabs(["📥 邮件管理", "⏰ 已超时", "📄 每日汇总"])

# ============ 标签页 1：邮件管理（按学院 / 类型筛选 + 查看 / 修改 / 删除） ============

with tab_list:
    f_col1, f_col2, f_col3 = st.columns(3)
    with f_col1:
        selected_categories = st.multiselect("紧急程度", CATEGORIES, default=CATEGORIES)
    with f_col2:
        college_options = sorted({r.get("college") or "其他" for r in all_rows})
        selected_colleges = st.multiselect("学院", college_options, default=[])
    with f_col3:
        selected_types = st.multiselect("类型", EMAIL_TYPES, default=[])

    keyword = st.text_input(
        "搜索（主题 / 发件人 / 摘要 / 正文）", placeholder="输入关键字后自动过滤"
    )

    def match(r):
        if selected_categories and r["category"] not in selected_categories:
            return False
        if selected_colleges and (r.get("college") or "其他") not in selected_colleges:
            return False
        if selected_types and r.get("email_type") not in selected_types:
            return False
        if keyword:
            haystack = " ".join(
                str(r.get(k) or "") for k in ("subject", "sender", "summary", "body")
            )
            if keyword not in haystack:
                return False
        return True

    filtered = [r for r in all_rows if match(r)]
    editor_key = "all_%s_%s_%s_%s" % (
        "|".join(selected_categories),
        "|".join(selected_colleges),
        "|".join(selected_types),
        keyword,
    )
    render_panel(filtered, editor_key)

# ============ 标签页 2：已超时（截止时间已过） ============

with tab_overdue:
    overdue_rows = [r for r in all_rows if db.is_overdue(r.get("deadline"))]
    st.caption(f"截止时间早于当前的邮件，共 {len(overdue_rows)} 封。可在右侧预览原文，或在左侧改截止时间/分类。")

    overdue_keyword = ""
    if overdue_rows:
        overdue_keyword = st.text_input(
            "在已超时邮件中搜索", placeholder="主题 / 发件人 / 摘要", key="overdue_keyword"
        )
        if overdue_keyword:
            overdue_rows = [
                r for r in overdue_rows
                if overdue_keyword
                in " ".join(str(r.get(k) or "") for k in ("subject", "sender", "summary"))
            ]

    render_panel(overdue_rows, f"overdue_{overdue_keyword}")

# ============ 标签页 3：每日汇总 ============

with tab_summary:
    col_a, col_b = st.columns([1, 3])
    if col_a.button("📝 重新生成 每日汇总.md", width="stretch"):
        rows = db.list_emails(categories=job.KEY_CATEGORIES)
        job.write_summary_md(rows)
        st.success("已重新生成")

    summary_rows = db.list_emails(categories=job.KEY_CATEGORIES)
    markdown = job.build_markdown(summary_rows)
    st.download_button(
        "⬇️ 下载 Markdown",
        data=markdown,
        file_name="每日汇总.md",
        mime="text/markdown",
        width="content",
    )
    st.caption(
        "汇总范围：全部「紧急待办」「重要阅读」，已超时的单独归入「已超时」栏目，"
        f"共 {len(summary_rows)} 封"
    )
    st.markdown(markdown)

    if os.path.exists(job.OUTPUT_FILE):
        st.caption(f"本地文件：{job.OUTPUT_FILE}")
