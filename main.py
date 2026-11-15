# -*- coding: utf-8 -*-
"""
邮件自动分类脚本
------------------------------------------------------------
流程：
1. 通过 IMAP 登录邮箱，只读取收件箱中【未读】的邮件
2. 解析发件人、主题、正文（处理中文乱码）
3. 调用大模型（OpenAI 兼容接口）返回 JSON：分类 / 摘要 / 待办事项
4. 把「紧急待办」和「重要阅读」的邮件汇总成 Markdown，写入 每日汇总.md

需要修改的地方都在下面的「配置区」，搜索 TODO 即可找到。
"""

import imaplib
import json
import os
import re
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header
from email.utils import parseaddr

from openai import OpenAI

import config
import db

# 先把 .env 中的配置读进环境变量，下面的 os.getenv 才能取到
config.load_env()

# ============================ 配置区（TODO：按需修改） ============================

# 邮箱账号（不写死个人信息，优先从环境变量读取；也可以直接在这里填自己的信息）
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT", "")   # 例如 "your_email@example.com"
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "") # 邮箱授权码（不是登录密码）
IMAP_SERVER = os.getenv("IMAP_SERVER", "mail.buaa.edu.cn")   # IMAP 服务器地址
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))               # IMAP SSL 端口

# 大模型配置（OpenAI 兼容接口）
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")     # 不设默认值，例如 "https://api.deepseek.com"
LLM_API_KEY = os.getenv("LLM_API_KEY", "")       # 例如 "sk-xxxxxxxx"
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# 汇总文件输出路径（默认当前目录下的 每日汇总.md）
OUTPUT_FILE = os.path.join(os.getcwd(), "每日汇总.md")

# 单封邮件正文最多送给模型的字符数，避免超长邮件浪费 token
MAX_BODY_CHARS = 3000

# 需要汇总进 Markdown 的分类
KEY_CATEGORIES = ("紧急待办", "重要阅读")

# 紧急程度（保留原有四档）
CATEGORIES = ("紧急待办", "重要阅读", "通知", "垃圾")

# 邮件类型（按内容性质划分，与提示词保持一致）
EMAIL_TYPES = (
    "讲座通知",
    "学术会议",
    "比赛通知",
    "老师回信",
    "行政通知",
    "招生招聘",
    "期刊推送",
    "其他",
)

# ============================== 工具函数 ==============================


def decode_mime_header(raw_value):
    """解码邮件头部字段（主题、发件人等），处理中文乱码。

    decode_header 会把编码后的字符串拆成 [(bytes/str, charset), ...]，
    这里逐段用对应字符集解码后再拼接。
    """
    if not raw_value:
        return ""

    parts = decode_header(raw_value)
    result = []
    for content, charset in parts:
        if isinstance(content, bytes):
            # 个别邮件 charset 缺失或写错（如 unknown-8bit），做兜底
            encoding = charset or "utf-8"
            try:
                result.append(content.decode(encoding, errors="ignore"))
            except (LookupError, UnicodeDecodeError):
                result.append(content.decode("utf-8", errors="ignore"))
        else:
            result.append(content)
    return "".join(result).strip()


def extract_sender(msg):
    """从 From 头中提取「显示名 <邮箱>」形式，并解码中文显示名。"""
    name, addr = parseaddr(msg.get("From", ""))
    name = decode_mime_header(name)
    if name and addr:
        return f"{name} <{addr}>"
    return addr or name


def extract_body(msg):
    """提取邮件正文（优先纯文本，其次 HTML 去标签），处理编码与传输编码。"""
    text_parts = []
    html_parts = []

    # walk() 会遍历所有 MIME 部件，包括 multipart/alternative 里的子部件
    for part in msg.walk():
        # 跳过容器部件（multipart/*）和附件
        if part.get_content_maintype() == "multipart":
            continue
        if part.get("Content-Disposition") and "attachment" in part.get("Content-Disposition"):
            continue

        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue

        # get_content() 会按 charset 自动解码，比手动 decode 更省事；
        # 个别邮件 charset 出错时兜底用 utf-8 / gbk 解码
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            content = payload.decode(charset, errors="ignore")
        except LookupError:
            content = payload.decode("utf-8", errors="ignore")

        if content_type == "text/plain":
            text_parts.append(content)
        else:
            html_parts.append(content)

    body = "\n".join(text_parts).strip()
    if not body and html_parts:
        # 没有纯文本时，简单去掉 HTML 标签
        html = "\n".join(html_parts)
        html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
        html = re.sub(r"(?s)<[^>]+>", " ", html)
        html = re.sub(r"&nbsp;", " ", html)
        html = re.sub(r"[ \t]+", " ", html)
        body = html.strip()

    return body[:MAX_BODY_CHARS]


def fetch_unread_emails(account=None, password=None, server=None, port=None):
    """登录 IMAP，只取收件箱中未读邮件，返回解析后的邮件列表。

    参数默认取配置区的值，可视化界面里可以传入自己填写的连接信息。
    """
    account = account or EMAIL_ACCOUNT
    password = password if password is not None else EMAIL_PASSWORD
    server = server or IMAP_SERVER
    port = port or IMAP_PORT

    emails = []

    # 993 端口使用 SSL
    imap = imaplib.IMAP4_SSL(server, port)
    try:
        imap.login(account, password)
        imap.select("INBOX")  # 选择收件箱（只读也可用 readonly=True）

        # UNSEEN 即「未读」；这里没用 readonly=True，
        # 但因为下面取正文用的是 BODY.PEEK[]，所以不会把邮件标记为已读
        status, data = imap.search(None, "UNSEEN")
        if status != "OK":
            print("搜索未读邮件失败：", status)
            return emails

        mail_ids = data[0].split()
        print(f"共找到 {len(mail_ids)} 封未读邮件")

        for mail_id in mail_ids:
            # PEEK 表示只查看不改变已读状态
            status, msg_data = imap.fetch(mail_id, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            raw_email = msg_data[0][1]
            msg = message_from_bytes(raw_email)

            subject = decode_mime_header(msg.get("Subject", "(无主题)"))
            sender = extract_sender(msg)

            # 邮件唯一标识，用于入库去重；个别邮件没有 Message-ID，用发件人+主题兜底
            message_id = (msg.get("Message-ID") or "").strip() or f"{sender}|{subject}"

            emails.append({
                "message_id": message_id,
                "sender": sender,
                "subject": subject,
                "body": extract_body(msg),
                "mail_date": (msg.get("Date") or "").strip(),
            })
    finally:
        # 无论成功失败都要退出登录
        try:
            imap.logout()
        except Exception:
            pass

    return emails


# ============================== 大模型分类 ==============================

# 提示词：强制模型只输出 JSON，字段固定
SYSTEM_PROMPT = """你是一个邮件分类助手。请阅读邮件内容，只输出一个 JSON 对象，不要输出任何其他文字、解释或代码块标记。

JSON 字段要求：
- "college": 邮件来自哪个学院或单位，填最简短的官方名称，例如 "航空学院"、"沈元学院"、"科学技术研究院"；无法判断填 "其他"
- "email_type": 邮件类型，只能是以下值之一：讲座通知、学术会议、比赛通知、老师回信、行政通知、招生招聘、期刊推送、其他
- "category": 紧急程度，只能是以下四个值之一：紧急待办、重要阅读、通知、垃圾
- "summary": 邮件摘要，50 个汉字以内
- "todo": 需要我执行的待办事项，用一句话描述；如果邮件没有明确待办则填 null
- "deadline": 邮件中明确写出的截止时间（报名截止、提交截止、活动开始等），格式 "YYYY-MM-DD HH:MM"；只有日期就填 "YYYY-MM-DD"；年份必须与邮件本身的时间一致（邮件里常只写月日，请按邮件时间补全年份）；没有明确时间填 null

类型判断标准：
- 讲座通知：学术讲座、论坛、报告会、大讲堂
- 学术会议：会议征稿、研讨会、专委会会议
- 比赛通知：学科竞赛、创新创业大赛、作品征集
- 老师回信：老师、导师或同事针对具体事项的一对一回复
- 行政通知：放假、报销、系统维护、账号认证等事务性通知
- 招生招聘：招生简章、招聘、人才计划
- 期刊推送：期刊目录、电子刊、学报推送
- 其他：无法归入以上类型

紧急程度判断标准：
- 紧急待办：有明确截止时间或需要我立刻处理/回复的事情
- 重要阅读：值得认真阅读的重要信息（项目、学业、会议、通知性但重要）
- 通知：一般性通知、公告、系统消息
- 垃圾：广告、推广、营销、可忽略的内容"""


def normalize_deadline(value):
    """把模型返回的截止时间统一成 YYYY-MM-DD HH:MM，识别不了就返回空字符串。"""
    if not value:
        return ""
    text = str(value).strip().replace("/", "-").replace("T", " ")
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", text)
    if not match:
        return ""
    year, month, day, hour, minute = match.groups()
    if hour is None:
        # 只有日期时，按当天 23:59 截止
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d} 23:59"
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d} {int(hour):02d}:{int(minute):02d}"


def classify_email(client, email_item):
    """调用大模型对单封邮件分类，返回 dict：college / email_type / category / summary / todo / deadline。"""
    user_content = (
        f"发件人：{email_item['sender']}\n"
        f"主题：{email_item['subject']}\n"
        f"正文：\n{email_item['body']}"
    )

    # 1) 调用模型。网络 / 鉴权 / 模型名错误都在这一步，单独提示，方便定位配置问题
    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0,  # 分类任务用 0，结果更稳定
        )
        content = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        # 单封失败不影响整体流程，但要如实写出失败原因（如 APIConnectionError、401 等）
        print(f"  [接口调用失败] {type(exc).__name__}: {exc}")
        return {
            "college": "其他",
            "email_type": "其他",
            "category": "通知",
            "summary": f"接口调用失败：{type(exc).__name__}",
            "todo": None,
            "deadline": "",
        }

    # 2) 从返回内容里取 JSON（模型偶尔会包一层 ```json 或前后带说明文字）
    data = None
    try:
        cleaned = re.sub(r"```(?:json)?", "", content).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        print(f"  [JSON 解析失败] {exc}；模型返回：{content[:200]}")

    if not isinstance(data, dict):
        print(f"  [格式异常] 模型返回：{content[:200]}")
        return {
            "college": "其他",
            "email_type": "其他",
            "category": "通知",
            "summary": "模型返回格式异常",
            "todo": None,
            "deadline": "",
        }

    # 3) 字段校验，避免模型返回缺字段或非法取值
    category = data.get("category")
    if category not in CATEGORIES:
        category = "通知"

    email_type = (data.get("email_type") or "").strip()
    if email_type not in EMAIL_TYPES:
        email_type = "其他"

    college = (data.get("college") or "").strip().strip("【】") or "其他"

    return {
        "college": college,
        "email_type": email_type,
        "category": category,
        "summary": (data.get("summary") or "").strip(),
        "todo": data.get("todo") or None,
        "deadline": normalize_deadline(data.get("deadline")),
    }


# ============================== 生成 Markdown ==============================


def _markdown_item(index, item):
    """渲染单封邮件的 Markdown 片段。"""
    lines = [f"### {index}. {item['subject']}"]
    lines.append(f"- **发件人**：{item['sender']}")
    if item.get("college"):
        lines.append(f"- **学院/单位**：{item['college']}")
    if item.get("email_type"):
        lines.append(f"- **类型**：{item['email_type']}")
    lines.append(f"- **摘要**：{item['summary']}")
    lines.append(f"- **待办**：{item['todo'] or '无'}")
    if item.get("deadline"):
        lines.append(f"- **截止时间**：{item['deadline']}")
    lines.append("")
    return lines


def build_markdown(results):
    """把邮件按「已超时 / 紧急待办 / 重要阅读」汇总成 Markdown 字符串。

    超过截止时间的邮件单独放进「已超时」栏目，不再计入紧急待办。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# 每日邮件汇总（{today}）", ""]

    rows = [dict(r) for r in results]
    # 已超时：有截止时间且早于当前时间
    overdue = [r for r in rows if db.is_overdue(r.get("deadline"))]
    # 紧急待办 / 重要阅读：排除已超时的
    urgent = [
        r for r in rows
        if r["category"] == "紧急待办" and not db.is_overdue(r.get("deadline"))
    ]
    reading = [
        r for r in rows
        if r["category"] == "重要阅读" and not db.is_overdue(r.get("deadline"))
    ]

    if not (overdue or urgent or reading):
        lines.append("> 今日没有已超时、紧急待办或重要阅读的邮件。")
        return "\n".join(lines)

    for title, items in (("已超时", overdue), ("紧急待办", urgent), ("重要阅读", reading)):
        if not items:
            continue
        lines.append(f"## {title}")
        lines.append("")
        for i, item in enumerate(items, 1):
            lines.extend(_markdown_item(i, item))

    return "\n".join(lines).rstrip()


def write_summary_md(rows, path=None):
    """把邮件列表渲染成 Markdown 并写入文件，返回 Markdown 内容。"""
    path = path or OUTPUT_FILE
    markdown = build_markdown(rows)
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)
    print(f"汇总已写入：{path}")
    return markdown


def process_emails(emails, client, on_progress=None):
    """逐封分类并写入数据库。

    on_progress: 可选回调 on_progress(序号, 总数, 邮件)，供可视化界面刷新进度。
    返回 (新增条数, 重复跳过条数)。
    """
    new_count, skip_count = 0, 0
    total = len(emails)

    for idx, email_item in enumerate(emails, 1):
        if on_progress:
            on_progress(idx, total, email_item)

        print(f"[{idx}/{total}] 正在分类：{email_item['subject']}")
        result = classify_email(client, email_item)
        email_item.update(result)

        record = {
            "message_id": email_item["message_id"],
            "sender": email_item["sender"],
            "subject": email_item["subject"],
            "body": email_item["body"],
            "category": result["category"],
            "summary": result["summary"],
            "todo": result["todo"],
            "college": result["college"],
            "email_type": result["email_type"],
            "deadline": result["deadline"],
            "mail_date": email_item.get("mail_date", ""),
        }
        is_new, _ = db.save_email(record)
        if is_new:
            new_count += 1
        else:
            skip_count += 1
            print("  -> 数据库中已存在，跳过")
        print(f"  -> [{result['college']}/{result['email_type']}] {result['category']}：{result['summary']}")

    return new_count, skip_count


# ============================== 主流程 ==============================


def main():
    # 0. 初始化数据库（建表）
    db.init_db()

    # 1. 读取未读邮件
    emails = fetch_unread_emails()
    if not emails:
        print("没有未读邮件，结束。")
        return

    # 2. 初始化大模型客户端（base_url 未配置时直接提示，避免请求打到默认地址）
    if not LLM_BASE_URL:
        print("未配置 LLM_BASE_URL，请设置环境变量或在界面侧边栏中填写后重试。")
        return
    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY or "EMPTY")

    # 3. 逐封分类并入库
    new_count, skip_count = process_emails(emails, client)
    print(f"\n入库完成：新增 {new_count} 封，重复跳过 {skip_count} 封")

    # 4. 从数据库读取「紧急待办 / 重要阅读」，生成 Markdown 汇总
    #    不再限制「今天入库」，这样才能把历史遗留、已超时的邮件也列出来
    rows = db.list_emails(categories=KEY_CATEGORIES)
    write_summary_md(rows)

    # 顺便在控制台打印分类统计
    print("分类统计：", db.count_by_category())


if __name__ == "__main__":
    main()
