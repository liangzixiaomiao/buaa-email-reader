# -*- coding: utf-8 -*-
"""
本地配置读写：从 .env 读取配置；在界面里修改后写回 .env。

.env 已加入 .gitignore，不会被提交到 Git；仓库里只保留 .env.example 作为模板。
"""

import os

# .env 文件路径（与脚本同目录）
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# 统一管理的配置项及默认值（顺序即写入 .env 的顺序）
KEYS = [
    ("EMAIL_ACCOUNT", ""),
    ("EMAIL_PASSWORD", ""),
    ("IMAP_SERVER", "mail.buaa.edu.cn"),
    ("IMAP_PORT", "993"),
    ("LLM_BASE_URL", ""),
    ("LLM_API_KEY", ""),
    ("LLM_MODEL", "deepseek-chat"),
]
DEFAULTS = dict(KEYS)


def _parse(path):
    """解析 .env，返回键值字典；忽略空行和 # 注释。"""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_env(path=None):
    """把 .env 中的配置读进 os.environ。

    真实环境变量优先级更高，已有的键不会被 .env 覆盖。
    """
    path = path or ENV_FILE
    for key, value in _parse(path).items():
        os.environ.setdefault(key, value)


def get_settings():
    """返回当前生效的配置（环境变量优先，其次默认值）。"""
    return {key: os.getenv(key, default) for key, default in KEYS}


def save_env(values, path=None):
    """把配置写回 .env，并同步到当前进程的环境变量，改完立即生效。"""
    path = path or ENV_FILE
    merged = get_settings()
    merged.update({k: str(v) for k, v in values.items() if k in DEFAULTS})

    lines = ["# 本地配置（包含账号与密钥，已被 .gitignore 忽略，不要提交）"]
    lines += [f"{key}={merged[key]}" for key, _ in KEYS]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    for key in DEFAULTS:
        os.environ[key] = merged[key]


def is_first_run():
    """判断是否首次使用：缺 .env，或邮箱 / base_url / api_key 还没填。"""
    if not os.path.exists(ENV_FILE):
        return True
    settings = get_settings()
    return not all(settings[k] for k in ("EMAIL_ACCOUNT", "LLM_BASE_URL", "LLM_API_KEY"))
