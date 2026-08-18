# download-ebook — 下载电子书脚本 (中英双语)

简要说明（中文）
- 一个轻量脚本，用于从目录页抓取章节链接并批量下载章节文件，同时更新本地保存的目录文件中的链接。

快速开始：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt  # 或者: pip install requests
python download-ebook.py -cfg ./lgqm.json
```

主要功能：
- 下载 `base_url` 指定的目录页并保存为 `save_dir/save_content_file`。
- 使用 `section_pattern`（正则）提取章节文件编号与标题。
- 根据 `file_url_template` 构造章节 URL 并下载章节文件。
- 将本地目录文件中的原始链接替换为下载后的文件名。

配置项说明（JSON 格式）：
- `base_url`（字符串，必填）：列出章节链接的目录页面，建议以 `/` 结尾。
- `save_dir`（字符串，必填）：保存章节文件和目录文件的目录。
- `section_pattern`（字符串，必填）：用于 `re.findall()` 的正则，必须捕获两个分组：数字文件名部分与标签文本。例如（忽略大小写）：

  `(?i)<a\\s+[^>]*?href=\\"(\\d+)\\.htm\\"[^>]*>([^<]+)</a>`

  返回类似 `( "009", "章节标题..." )` 的元组。
- `file_url_template`（字符串，必填）：构造章节 URL 的模板，支持 `{base_url}`, `{m1}`, `{m2}`，例如：`{base_url}{m1}.htm`。
- `save_content_file`（字符串，可选）：保存目录页的文件名（默认：`目录.html`）。
- `save_file`（字符串，可选）：保存章节文件名的模板（支持 `{m1}` 和 `{m2}`，默认：`{m1}-{m2}.html`）。
- `delay_on_error`（整数，可选）：发生错误时的等待秒数（默认：20）。

编码与 `#sym:encoding` 标记：
- 脚本优先尝试 `gbk` 解码，回退到 `utf-8`。如果无法可靠判断编码，会在保存的目录文件顶部插入注释标记：

  `<!-- #sym:encoding utf-8 -->`

  以便后续运行时能稳定地解码/写回该文件。

请求头与回退：
- 脚本会发送常见浏览器头部，若 `urllib` 请求失败，会尝试使用 `requests` 回退（建议安装 `requests`）。

常见问题排查：
- 浏览器能打开但脚本返回 404：检查 `base_url` 是否多了 `index.htm/` 或尾部斜杠；优先使用目录形式 `http://site/dir/`。
- 没有匹配到章节：调整 `section_pattern`，确保使用了像上面示例的忽略大小写模式来匹配 `A HREF` 等变体。
- 回退 `requests` 未安装：安装 `pip install requests`。

----

Brief (English)
- Lightweight script to fetch a directory page, extract chapter links, download each chapter, and rewrite the saved TOC to local filenames.

Quick start:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt  # or: pip install requests
python download-ebook.py -cfg ./lgqm.json
```

Main behavior:
- Saves the `base_url` page to `save_dir/save_content_file`.
- Uses `section_pattern` to find chapter file numbers and titles.
- Builds chapter URLs with `file_url_template` and downloads each file.
- Rewrites links in the saved table-of-contents file to point to downloaded filenames.

Config keys (JSON):
- `base_url`: directory/index URL listing chapters (string, required).
- `save_dir`: output directory (string, required).
- `section_pattern`: regex used with `re.findall()` that must capture two groups: numeric filename and link text. Example (case-insensitive):

  `(?i)<a\\s+[^>]*?href=\\"(\\d+)\\.htm\\"[^>]*>([^<]+)</a>`

- `file_url_template`: template for chapter URL, use `{base_url}`, `{m1}`, `{m2}`.
- `save_content_file`, `save_file`, `delay_on_error`: optional settings as described above.

Encoding note:
- Script prefers `gbk` then `utf-8`. It may insert `<!-- #sym:encoding utf-8 -->` at the top of saved content files to record chosen encoding.

Troubleshooting:
- 404 while browser can open URL: check trailing slash and prefer directory-style `base_url`.
- No matches: adjust `section_pattern` to a case-insensitive, flexible pattern.
- Install `requests` if you want the fallback: `pip install requests`.

Examples and sample configs: see `lgqm.json`, `sgyy.json`.

Use responsibly; respect website terms and robots.txt.
