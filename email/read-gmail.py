#!/usr/bin/env python3
"""Download all Gmail messages from one day as text JSON files."""

import argparse
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

# Allow the script to use the shared helper and logger from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from logging_helper import get_logger


def load_shared_module():
    path = Path(__file__).with_name("read-email.py")
    spec = importlib.util.spec_from_file_location("read_email_shared", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载共享模块: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shared = load_shared_module()
logger = get_logger("gmail")


def main():
    parser = argparse.ArgumentParser(description="按日期下载 Gmail 邮件为 JSON")
    parser.add_argument("email_address")
    parser.add_argument("date", help="日期，格式 YYYY-MM-DD")
    parser.add_argument(
        "-t",
        "--token-env",
        default="GMAIL_APP_PASSWORD",
        help="存放 Gmail App Password 的环境变量名，默认 GMAIL_APP_PASSWORD",
    )
    parser.add_argument(
        "-p",
        "--print",
        action="store_true",
        dest="print_log",
        help="同时将日志输出到终端",
    )
    args = parser.parse_args()
    if args.print_log:
        shared.enable_terminal_logging(logger)
    try:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        saved = shared.read_day(
            args.email_address,
            target_date,
            "imap.gmail.com",
            args.token_env,
            logger,
            "Gmail",
        )
        logger.info("完成，共保存 %d 封邮件", saved)
    except (ValueError, RuntimeError, shared.imaplib.IMAP4.error, OSError) as error:
        logger.error("读取 Gmail 失败: %s", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
