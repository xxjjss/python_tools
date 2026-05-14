"""
Cleanup email script - 删除过往邮件

Usage:
    python3 cleanup-email.py -cfg xxjjs_ca.json         # 交互确认
    python3 cleanup-email.py -cfg xxjjs_ca.json -f       # 强制删除

配置文件: xxjjs_ca.json
认证逻辑:
  - Gmail/Yahoo 均使用 App Password
  - _resolve_password()
  - 支持 macOS keychain (security CLI)
"""
import json
import imaplib
import email
import re
import argparse
import sys
import os
import subprocess
import tty
import termios
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from dateutil import parser as date_parser

def getch():
    """Read a single character from stdin without waiting for Enter."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch

class EmailCleaner:
    def __init__(self, config_path, force=False, out_file=None):
        with open(config_path, 'r') as f:
            self.config = json.load(f)

        self.mail = None
        self.force = force
        self.folder = 'INBOX'
        self.out_file_path = out_file

        # Parse dates
        self.all_before = datetime.strptime(self.config['remove_all_before'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
        self.unimportant_before = datetime.strptime(self.config['remove_unimportant_before'], '%Y-%m-%d').replace(tzinfo=timezone.utc)

        self.providers = {
            "yahoo": "imap.mail.yahoo.com",
            "gmail": "imap.gmail.com"
        }
        # 可选的起始日期（早于该日期的邮件不会被处理）
        self.start_date = None
        cfg_start = self.config.get('start_date')
        if cfg_start:
            try:
                sd = date_parser.parse(cfg_start)
                if sd.tzinfo is None:
                    sd = sd.replace(tzinfo=timezone.utc)
                self.start_date = sd
            except Exception:
                # 忽略无法解析的 start_date
                self.start_date = None

    def log_msg(self, str):
        print(str)
        if self.out_file_path:
            try:
                with open(self.out_file_path, 'a', encoding='utf-8') as f:
                    f.write(str + "\n")
            except Exception:
                # 不要因为日志写入错误中断主流程
                pass

    # ──────────────────────────────────────────
    # 认证逻辑
    # ──────────────────────────────────────────
    def _resolve_password(self):
        """
        密码解析顺序:
          1. JSON 配置 > auth.secret (如果不是占位符)
          2. macOS 钥匙串 (security find-generic-password)
          3. ~/.secrets/ 目录下的对应文件
          4. ~/.zshrc 环境变量
        """
        cfg_secret = self.config.get('auth', {}).get('secret', '')
        email_addr = self.config['email_address']

        # 1) JSON 中配置了有效密码
        if cfg_secret and cfg_secret not in ('YOUR_APP_PASSWORD_HERE', 'ACCESS_TOKEN'):
            return cfg_secret

        # 2) macOS 钥匙串
        try:
            r = subprocess.run(
                ['security', 'find-generic-password', '-a', email_addr, '-w'],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass

        # 3) ~/.secrets/ 目录
        secrets_dir = os.path.expanduser('~/.secrets')
        if os.path.isdir(secrets_dir):
            for fname in os.listdir(secrets_dir):
                if email_addr.split('@')[0] in fname or 'app_password' in fname:
                    try:
                        with open(os.path.join(secrets_dir, fname)) as f:
                            content = f.read().strip()
                            # 可能是纯文本密码
                            if len(content) > 5 and ' ' not in content:
                                return content
                    except:
                        pass

        # 4) 从环境变量读取 (YAHOO_MAIL_TOKEN / GMAIL_APP_PASSWORD)
        email_user = email_addr.split('@')[0]
        for var in [f'{email_user.upper()}_MAIL_TOKEN', 'YAHOO_MAIL_TOKEN', 'GMAIL_APP_PASSWORD']:
            val = os.environ.get(var)
            if val and val.strip():
                return val.strip()

        return None

    def check_auth(self):
        """
        [TODO 完成] 授权配置说明:
          - Yahoo/Gmail 均需使用 App Password
          - 本方法将按优先级查找密码，连接到 IMAP 服务器，
            并以 READ-WRITE 模式选择 INBOX 以确认全部权限。
          - 连接失败或权限不足时打印错误并返回 False。
        """
        provider = self.config.get("service_provider", "yahoo").lower()
        host = self.providers.get(provider)

        if not host:
            print(f"[-] 不支持的邮箱服务商: {provider}（支持: yahoo, gmail）")
            return False

        # 解析密码
        pwd = self._resolve_password()
        if not pwd:
            print(f"[-] 未能找到 {self.config['email_address']} 的 App Password/Token")
            print("    Gmail: https://myaccount.google.com/apppasswords")
            print("    Yahoo: 账号安全 > 生成应用TOKEN")
            return False

        # 连接 & 登录
        try:
            self.mail = imaplib.IMAP4_SSL(host)
            self.mail.login(self.config['email_address'], pwd)
        except imaplib.IMAP4.error as e:
            print(f"[-] IMAP 登录失败: {e}")
            return False
        except Exception as e:
            print(f"[-] 连接失败: {e}")
            return False

        # 以 READ-WRITE 模式选择 INBOX，验证写权限
        try:
            typ, data = self.mail.select('INBOX')
            if typ != 'OK':
                print(f"[-] 无法选择 INBOX: {data}")
                return False
            # IMAP select 默认 READ-WRITE，无需额外操作
            print(f"[+] 连接成功 ({provider})，INBOX 邮件数: {len(data[0].split()) if data[0] else 0}")
            return True
        except Exception as e:
            print(f"[-] INBOX 访问失败: {e}")
            return False

    # ──────────────────────────────────────────
    # 邮件辅助方法
    # ──────────────────────────────────────────
    def decode_mime_header(self, header):
        if not header:
            return ""
        decoded = decode_header(header)
        parts = []
        for content, charset in decoded:
            if isinstance(content, bytes):
                try:
                    parts.append(content.decode(charset or 'utf-8', errors='replace'))
                except LookupError:
                    parts.append(content.decode('utf-8', errors='replace'))
            else:
                parts.append(content)
        return "".join(parts)

    def has_attachment(self, msg):
        """
        针对 Authentisign 等复杂嵌套邮件优化的附件检测逻辑。
        """
        # 1. 如果不是多部分邮件，绝对没有附件
        if not (msg.is_multipart() or msg.get_content_type().__contains__('multipart') or msg.get_content_maintype().__contains__('multipart')):
            return False

        for part in msg.walk():
            # 2. 跳过容器类型
            content_type = part.get_content_type()
            if part.get_content_maintype() == 'multipart':
                continue

            # 3. 提取文件名（处理各种编码情况）
            filename = part.get_filename()
            if filename:
                filename = self.decode_mime_header(filename)
            
            # 4. 提取布局属性
            disposition = str(part.get("Content-Disposition", "")).lower()

            # --- 核心判定逻辑 ---

            # A: 只要有明确的文件名，且不是正文类型，就视为附件
            # 排除 text/plain 和 text/html 是为了防止某些客户端把正文也带上文件名
            if filename and content_type not in ['text/plain', 'text/html']:
                return True

            # B: 明确标记为 attachment 的部分
            if 'attachment' in disposition:
                return True

            # C: 针对 Authentisign 这种特殊情况：
            # 有些 PDF 附件在 MIME 中可能被标记为 application/pdf 但没有 disposition
            if content_type == 'application/pdf' or 'pdf' in filename.lower() if filename else False:
                return True

        return False

    def has_attachment_from_structure(self, structure):
        """从 IMAP BODYSTRUCTURE 检测附件，兼容多种嵌套 tuple/list 格式"""
        if not isinstance(structure, (list, tuple)):
            return False

        def walk(s):
            if isinstance(s, (list, tuple)):
                for item in s:
                    # 递归处理子结构
                    if isinstance(item, (list, tuple)):
                        if walk(item):
                            return True
                    # bytes 片段可能包含 disposition/name 等信息
                    elif isinstance(item, bytes):
                        try:
                            text = item.decode('utf-8', errors='ignore').lower()
                            if 'attachment' in text or 'filename' in text or 'name=' in text:
                                return True
                        except Exception:
                            continue
                    elif isinstance(item, str):
                        if 'attachment' in item.lower() or 'filename' in item.lower() or 'name=' in item.lower():
                            return True
                return False
            return False

        try:
            return walk(structure)
        except Exception:
            return False

    def get_attachment_names_from_structure(self, structure):
        """从 BODYSTRUCTURE 提取附件文件名列表（如果能解析到）"""
        names = []
        if not isinstance(structure, (list, tuple)):
            return names

        def walk(s):
            if isinstance(s, (list, tuple)):
                for item in s:
                    if isinstance(item, (list, tuple)):
                        walk(item)
                    elif isinstance(item, bytes):
                        try:
                            text = item.decode('utf-8', errors='ignore')
                            # 查找 filename="..." 或 name="..."
                            for m in re.finditer(r'filename\s*=\s*"?([^";\)]+)"?', text, flags=re.IGNORECASE):
                                names.append(m.group(1).strip())
                            for m in re.finditer(r'name\s*=\s*"?([^";\)]+)"?', text, flags=re.IGNORECASE):
                                names.append(m.group(1).strip())
                        except Exception:
                            continue
                    elif isinstance(item, str):
                        for m in re.finditer(r'filename\s*=\s*"?([^";\)]+)"?', item, flags=re.IGNORECASE):
                            names.append(m.group(1).strip())
                        for m in re.finditer(r'name\s*=\s*"?([^";\)]+)"?', item, flags=re.IGNORECASE):
                            names.append(m.group(1).strip())

        try:
            walk(structure)
        except Exception:
            pass

        # 去重并返回
        clean = []
        for n in names:
            if n and n not in clean:
                clean.append(n)
        return clean

    def get_attachment_names_from_msg(self, msg):
        """从 email.message.Message 提取附件文件名列表"""
        names = []
        if not msg:
            return names
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            filename = part.get_filename()
            if filename:
                try:
                    filename = self.decode_mime_header(filename)
                except Exception:
                    pass
                if filename and filename not in names:
                    names.append(filename)
        return names

    def is_reply(self, msg):
        """判断是否为回复邮件"""
        subject = self.decode_mime_header(msg.get("Subject", "")).strip()
        # 标准回复前缀
        if re.match(r'^(Re|RE|Fwd|FWD|回复|转发|回覆|轉寄)[:\s\]\[]', subject):
            return True
        # IMAP 标准头
        if msg.get("In-Reply-To") or msg.get("References"):
            return True
        return False

    def is_important_sender(self, sender_email):
        """
        配置中的 important_sender 支持正则表达式。
        例: ".*@embark\\.ca" 保留所有 embark.ca 的发件人。
        """
        for pattern in self.config.get('important_sender', []):
            try:
                if re.search(pattern, sender_email, re.IGNORECASE):
                    return True
            except re.error as e:
                print(f"[-] 正则错误: {pattern} -> {e}")
        return False

    def _parse_robust_date(self, date_str):
        """
        鲁棒地解析邮件日期，处理非标准格式如 "11/17/2010"
        """
        if not date_str:
            return None
            
        try:
            # 1. 尝试标准 RFC 2822 解析
            return parsedate_to_datetime(date_str)
        except Exception:
            try:
                # 2. 尝试使用 dateutil 自动识别 (处理 11/17/2010 等)
                dt = date_parser.parse(date_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                # 3. 最后的保底：尝试常见的非标准格式
                for fmt in ("%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y"):
                    try:
                        # 提取字符串中的日期部分（忽略时区后缀）
                        clean_date = re.search(r'\d{1,4}[/-]\d{1,4}[/-]\d{1,4}', date_str)
                        if clean_date:
                            dt = datetime.strptime(clean_date.group(), fmt)
                            return dt.replace(tzinfo=timezone.utc)
                    except:
                        continue
        return None

    def is_important_subject(self, subject):
        """
        配置中的 important_email_subjects 支持正则表达式。
        例: "City of Bellevue, WA Invoice" 保留该标题的邮件。
        """
        for pattern in self.config.get('important_email_subjects', []):
            try:
                if re.search(pattern, subject, re.IGNORECASE):
                    return True
            except re.error as e:
                print(f"[-] 正则错误: {pattern} -> {e}")
        return False

    def is_unimportant_subject(self, subject):
        """
        配置中的 unimportant_email_subjects 支持正则表达式。
        例: "Check in now" 匹配包含 "check in now" 的标题。
        """
        for pattern in self.config.get('unimportant_email_subjects', []):
            try:
                if re.search(pattern, subject, re.IGNORECASE):
                    return True
            except re.error as e:
                print(f"[-] 正则错误: {pattern} -> {e}")
        return False

    # ──────────────────────────────────────────
    # 扫描 & 删除
    # ──────────────────────────────────────────
    def run_cleanup(self):
        self.log_msg(f"[*] 模式: {'强制删除 (force)' if self.force else '交互确认'}")
        self.log_msg(f"[*] 全删日期: {self.all_before.date()}")
        self.log_msg(f"[*] 部分删除日期: {self.unimportant_before.date()}")
        if self.start_date:
            self.log_msg(f"[*] 起始搜索日期: {self.start_date.date()}")

        batch_size = self.config.get('batch_size', 10)
        print(f"[*] 批处理大小: {batch_size}")

        # 使用 IMAP SENTBEFORE 搜索（按发件日期而非接收日期）
        # 分两次搜索: 一次全删范围, 一次部分删除范围
        all_before_str = self.all_before.strftime("%d-%b-%Y")
        unimportant_before_str = self.unimportant_before.strftime("%d-%b-%Y")

        candidates = []  # [(mail_id, msg_date, sender_email, subject, reason)]

        # ── 搜索全删范围 ──
        if self.start_date:
            start_str = self.start_date.strftime("%d-%b-%Y")
            res, data = self.mail.search(None, f'(SENTSINCE {start_str} SENTBEFORE {all_before_str})')
        else:
            res, data = self.mail.search(None, f'(SENTBEFORE {all_before_str})')
        if res == 'OK':
            mail_ids = data[0].split()
            total = len(mail_ids)
            print(f"[*] 全删范围内邮件 (≤{self.all_before.date()}): {total} 封")
            for i in range(0, total, batch_size):
                batch = mail_ids[i:i+batch_size]
                print(f"[*] 处理批次 {i//batch_size + 1}: {len(batch)} 封邮件")
                self._classify_emails(batch, 'ALL', candidates)
                self.process_candidates(candidates)
                candidates = []  # 清空候选列表，为下一范围准备

        else:
            print(f"[-] 全删范围搜索失败")

        # ── 搜索部分删除范围 ──
        # 部分删除范围通常是 all_before ~ unimportant_before；如果配置了 start_date，
        # 则下界取两者中较晚的一个
        partial_since_dt = self.all_before
        if self.start_date and self.start_date > partial_since_dt:
            partial_since_dt = self.start_date
        partial_since_str = partial_since_dt.strftime("%d-%b-%Y")
        res, data = self.mail.search(None,
            f'(SENTSINCE {partial_since_str} SENTBEFORE {unimportant_before_str})')
        if res == 'OK':
            mail_ids = data[0].split()
            total = len(mail_ids)
            print(f"[*] 部分删除范围 ({self.all_before.date()} ~ {self.unimportant_before.date()}): {total} 封")
            for i in range(0, total, batch_size):
                batch = mail_ids[i:i+batch_size]
                print(f"[*] 处理批次 {i//batch_size + 1}: {len(batch)} 封邮件")
                self._classify_emails(batch, 'PARTIAL', candidates)
                self.process_candidates(candidates)
                candidates = []  # 清空候选列表，为下一范围准备

        else:
            print(f"[-] 部分删除范围搜索失败")

    def process_candidates(self, candidates):
        """处理候选邮件列表：显示、确认并删除"""
        if not candidates:
            return

        if self.force:
            print(f"共 {len(candidates)} 封邮件将被删除:")
            for i, (mid, d, sender, subj, reason) in enumerate(candidates, 1):
                log = f"[+] 删除邮件: {d.date()} {sender} | {subj[:60]} | {reason}"
                self.log_msg(log) 
                self.mail.store(mid, '+FLAGS', '\\Deleted')
            self.mail.expunge()
            print(f"[+] 已删除 {len(candidates)} 封邮件。")
  
        else: 
            for i, (mid, d, sender, subj, reason) in enumerate(candidates, 1):
                print(f"\n[{i}] 日期: {d.date()} | {sender}")
                print(f"    标题: {subj[:60]}")
                print(f"    原因: {reason}")
                print(f"  ==> [Y] 删除  [N] 跳过  [Q] 退出")
                confirm = getch().lower()

                if confirm == 'q':
                    print("\n[!] 用户终止操作。")
                    sys.exit(0)
                elif confirm == 'y':
                    self.mail.store(mid, '+FLAGS', '\\Deleted')
                    self.mail.expunge()
                    log = f"[+] 删除邮件: {sender} | {subj[:60]} | {d.date()} "
                    self.log_msg(log) 
                else:
                    log = f"[+] 跳过邮件: {sender} | {subj[:60]} | {d.date()} "
                    self.log_msg(log)

    def _classify_emails(self, mail_ids, scope, candidates):
        """将扫描到的邮件按规则分类"""
        fetch_failed = 0
        protected_mails = 0
        to_delete_mails = 0
        for mid in mail_ids:
            try:
                res, data = self.mail.fetch(mid, '(BODY.PEEK[HEADER] BODYSTRUCTURE)')
                if res != 'OK':
                    fetch_failed += 1
                    continue

                header_data = None
                bodystructure = None

                # data 的形态在不同服务器/库版本下略有不同，尝试多种解析策略
                for item in data:
                    if isinstance(item, tuple) and len(item) >= 2:
                        payload = item[1]
                        if isinstance(payload, bytes):
                            if header_data is None:
                                header_data = payload
                        elif isinstance(payload, (list, tuple)):
                            # 很可能是 BODYSTRUCTURE 的解析结果
                            if bodystructure is None:
                                bodystructure = payload
                    elif isinstance(item, bytes):
                        # 有时 header 直接作为 bytes 项出现在 data 中
                        if header_data is None and b'from:' in item[:50].lower():
                            header_data = item

                # 兜底：尝试老索引位置
                if header_data is None:
                    try:
                        header_data = data[0][1]
                    except Exception:
                        fetch_failed += 1
                        continue

                msg = email.message_from_bytes(header_data)
                subject = self.decode_mime_header(msg.get("Subject", ""))
                sender = self.decode_mime_header(msg.get("From", ""))
                _, sender_email = parseaddr(sender)

                # 解析日期
                dt = self._parse_robust_date(msg.get("Date"))
                if not dt:
                    fetch_failed += 1
                    continue

                # print(f"[*] 扫描邮件: {sender} | {subject[:60]} | 日期: {dt.date()}")

                # 优先使用 BODYSTRUCTURE 来检测附件（比只用 header 更可靠），并尝试解析文件名
                has_attachment = False
                attachment_names = []
                full_msg = None
                if bodystructure is not None:
                    try:
                        has_attachment = self.has_attachment_from_structure(bodystructure)
                        attachment_names = self.get_attachment_names_from_structure(bodystructure)
                    except Exception:
                        has_attachment = False
                        attachment_names = []

                # 如果没有 BODYSTRUCTURE，或 BODYSTRUCTURE 未检测到但 headers 显示 multipart，
                # 再抓取整封邮件解析真实 body（更可靠但更慢）。
                if not has_attachment and (msg.is_multipart() or 'multipart' in msg.get_content_type().lower()):
                    try:
                        res2, data2 = self.mail.fetch(mid, '(RFC822)')
                        if res2 == 'OK' and data2 and isinstance(data2[0], tuple) and data2[0][1]:
                            full_msg = email.message_from_bytes(data2[0][1])
                            has_attachment = self.has_attachment(full_msg)
                            # 如果之前没有从 structure 中找到文件名，尝试从完整邮件中提取
                            if not attachment_names:
                                attachment_names = self.get_attachment_names_from_msg(full_msg)
                    except Exception:
                        pass
                is_reply_msg = self.is_reply(msg)
                important = self.is_important_sender(sender_email)
                important_subject = self.is_important_subject(subject)

                # 保护规则: 附件 / 回复 / 重要发件人 / 重要标题 → 不删
                if has_attachment or is_reply_msg or important or important_subject:
                    reason = None
                    if has_attachment:
                        reason = '有附件'
                    elif is_reply_msg:
                        reason = '回复邮件'
                    elif important:
                        reason = '重要发件人'
                    else:
                        reason = '重要标题'
                    self.log_msg(f"[*] {reason}: {sender} | {subject[:60]} | {dt.date()}")
                    if reason == '有附件' and attachment_names:
                        for name in attachment_names:
                            self.log_msg(f"    附件名: {name}")
                    protected_mails += 1
                    continue

                if scope == 'ALL':
                    # 全删范围: 删除所有非保护的邮件
                    candidates.append((mid, dt, sender_email, subject, "过期邮件 (全删范围)"))
                    to_delete_mails += 1

                elif scope == 'PARTIAL':
                    # 部分删除范围: 仅删除标题匹配的
                    if self.is_unimportant_subject(subject):
                        self.log_msg(f"[*] 删除非重要标题: {sender} | {subject[:60]} | {dt.date()}") 
                        candidates.append((mid, dt, sender_email, subject, "不重要标题"))
                        to_delete_mails += 1
                    else:
                        self.log_msg(f"[*] 可删除邮件: {sender} | {subject[:60]} | {dt.date()}")
                else:
                    protected_mails += 1

            except Exception as e:
                print(f"[-] 处理邮件时出错: {e}")
                fetch_failed += 1
                continue
        print(f"[*] 批次结果: {len(candidates)} 封待删, {protected_mails} 封受保护, {fetch_failed} 封获取失败")

def main():
    parser = argparse.ArgumentParser(description="Email Cleanup Tool - 删除过往邮件")
    parser.add_argument("-cfg", "--config", required=True, help="Path to JSON config file")
    parser.add_argument("-f", "--force", action="store_true", help="Force deletion without confirmation")
    parser.add_argument("-o", "--output", help="Path to log file")

    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"[-] 配置文件不存在: {args.config}")
        sys.exit(1)
    
    if args.output and not os.path.exists(args.output):
        # create the file
        with open(args.output, 'w', encoding='utf-8') as f:
            pass

    cleaner = EmailCleaner(args.config, force=args.force, out_file=args.output)
    if cleaner.check_auth():
        cleaner.run_cleanup()
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
