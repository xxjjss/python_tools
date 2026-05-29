import argparse
try:
    from msvcrt import getch
except ImportError:
    import sys
    import tty
    import termios

    def getch():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch
import os
import re
import urllib.request
import sys
import time
import json


class ebook_downloader:
    def __init__(self, config: dict):
        self.base_url = config.get("base_url")
        self.save_dir = config.get("save_dir")
        self.section_pattern = config.get("section_pattern")
        # Template to build each chapter URL. Supports placeholders: {base_url}, {m1}, {m2}
        self.file_url_template = config.get("file_url_template")
        self.save_file_template = config.get("save_file", "{m1}-{m2}.html")  # Optional template for save file name
        self.save_content_file = config.get("save_content_file", "目录.html")  # Optional file name to save the main page content

        if not self.base_url or not self.save_dir or not self.section_pattern or not self.file_url_template:
            print("[-] 配置文件缺少必要字段，请确保包含 base_url, save_dir, section_pattern 和 file_url_template")
            sys.exit(1)
        if not self.base_url.endswith("/"):
            self.base_url += "/"
        self.delay_on_error = config.get("delay_on_error", 20)
        self.header = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}

        self.codec = None # 用于跟踪当前目录文件的编码
        self.current_content = None  # 用于跟踪当前目录文件的内容

    def read_content_page(self):
        print("正在获取目录页...")
        try:
            print(f"请求目录页 URL: {self.base_url}")
            # Add some common browser headers
            headers = dict(self.header)
            headers.update({
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Connection': 'keep-alive',
            })

            req = urllib.request.Request(self.base_url, headers=headers)

            with urllib.request.urlopen(req, timeout=10) as response:
                html_content = response.read()
                self.current_content = html_content  # 保存原始内容以供后续更新使用
                content_save_path = os.path.join(self.save_dir, self.save_content_file)

                if os.path.exists(content_save_path):
                    print(f"跳过目录文件: {self.save_content_file}")
                else:
                    with open(content_save_path, 'wb') as f:
                        f.write(html_content)
                        print(f"保存目录文件: {self.save_content_file}")

                try:
                    html_text = html_content.decode('gbk')
                    self.codec = 'gbk'
                except UnicodeDecodeError:
                    html_text = html_content.decode('utf-8', errors='ignore')
                    self.codec = 'utf-8'

                self.current_content = html_text  # 保存解码后的文本内容以供后续更新使用

                return html_text, content_save_path
        except Exception as e:
            # If HTTPError, show status and a snippet of body if possible
            try:
                import urllib.error
                if isinstance(e, urllib.error.HTTPError):
                    print(f"获取目录页【{self.base_url}】失败: HTTP {e.code} - {e.reason}")
                    try:
                        body = e.read().decode('utf-8', errors='ignore')
                        print(f"响应内容（前512字节）：\n{body[:512]}")
                    except Exception:
                        pass
                else:
                    print(f"获取目录页【{self.base_url}】失败: {e}")
            except Exception:
                print(f"获取目录页【{self.base_url}】失败: {e}")

            # Fallback: try requests if installed (better cookie/redirect handling)
            try:
                import requests
                print("尝试使用 requests 回退请求...")
                resp = requests.get(self.base_url, headers=headers, timeout=10, allow_redirects=True)
                print(f"requests 状态码: {resp.status_code}")
                if resp.status_code == 200:
                    html_content = resp.content
                    self.current_content = html_content
                    content_save_path = os.path.join(self.save_dir, self.save_content_file)
                    if os.path.exists(content_save_path):
                        print(f"跳过目录文件: {self.save_content_file}")
                    else:
                        with open(content_save_path, 'wb') as f:
                            f.write(html_content)
                            print(f"保存目录文件: {self.save_content_file}")

                    try:
                        html_text = html_content.decode('gbk')
                        self.codec = 'gbk'
                    except UnicodeDecodeError:
                        html_text = html_content.decode('utf-8', errors='ignore')
                        self.codec = 'utf-8'

                    self.current_content = html_text
                    return html_text, content_save_path
                else:
                    print(f"requests 回退也返回非200状态: {resp.status_code}")
            except Exception as e2:
                print(f"requests 回退失败: {e2}")

            sys.exit(1)

    def undate_link(self, content_save_path, section, new_link):
        # 构建原始链接格式，假设原链接是 {m1}.html
        ext = os.path.splitext(new_link)[1]
        old_link = f"{section}{ext}"  # 根据新链接的扩展名构建旧链接 
        self.current_content = self.current_content.replace(old_link, new_link)
        print(f"更新目录文件中的链接: {old_link} -> {new_link}")

        with open(content_save_path, 'w', encoding=self.codec, errors='ignore') as f:
                f.write(self.current_content)

    def download_novel_chapters(self):
        base_url = self.base_url
        save_dir = self.save_dir

        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
            print(f"创建目录: {save_dir}")

        html_text, content_save_path = self.read_content_page()

        # 正则匹配链接和章节名
        matches = re.findall(self.section_pattern, html_text)

        if not matches:
            print("未匹配到符合条件的章节链接，请检查网页源代码结构。")
            return

        print(f"共找到 {len(matches)} 个符合条件的章节。开始检查并下载...")

        # 初始化失败计数器
        failed_count = 0
        downloaded_count = 0

        for m1, m2 in matches:

            # Build file URL using template from config. Template may use {base_url}, {m1}, {m2}.
            file_url = self.file_url_template.format(base_url=self.base_url, m1=m1, m2=m2)

            # 过滤非法字符
            m2 = re.sub(r'[\\/:*?"<>|]', '_', m2)  # 替换文件名中的非法字符
            m1 = re.sub(r'[\\/:*?"<>|]', '_', m1)  # 替换文件名中的非法字符

            save_file = self.save_file_template.format(m1=m1, m2=m2)
            save_path = os.path.join(save_dir, save_file)

            # 如果文件已经存在，则忽略并跳过
            if os.path.exists(save_path):
                print(f"跳过已存在文件: {save_file}")
                self.undate_link(content_save_path, section=m1, new_link=save_file)  # 更新目录文件中的链接
                continue

            print(f"正在下载: {save_file} -> {file_url}")
            try:
                chapter_req = urllib.request.Request(file_url, headers=self.header)
                with urllib.request.urlopen(chapter_req, timeout=10) as ch_resp:
                    ch_content = ch_resp.read()
                    with open(save_path, 'wb') as f:
                        f.write(ch_content)
                        downloaded_count += 1
                        self.undate_link(content_save_path, section=m1, new_link=save_file)  # 更新目录文件中的链接
                # 成功下载后重置失败计数（可选）
                failed_count = 0
            except Exception as e:
                failed_count += 1
                print(f"下载失败 [{save_file}]: {e} (当前累计失败次数: {failed_count}/3)")
                time.sleep(self.delay_on_error)

                # 如果下载失败达到三次，则打印提示并彻底退出程序
                if failed_count >= 3:
                    print("\n❌ 错误：下载失败次数已达3次，可能网络出现问题或被网站封禁，程序自动退出。")
                    sys.exit(1)

        print(f"\n🎉 所有任务执行完毕！累计下载 {downloaded_count} 个章节")

def main():
    parser = argparse.ArgumentParser(description="Download ebook - 下载电子书")
    parser.add_argument("-cfg", "--config", required=True, help="Path to JSON config file")

    args = parser.parse_args()
    
    if not os.path.exists(args.config):
        print(f"[-] 配置文件不存在: {args.config}")
        sys.exit(1)
    # 读取 JSON 配置文件并传入 downloader
    try:
        with open(args.config, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception as e:
        print(f"[-] 无法读取配置文件: {e}")
        sys.exit(1)

    downloader = ebook_downloader(config=config)
    downloader.download_novel_chapters()
        

if __name__ == "__main__":
    main()