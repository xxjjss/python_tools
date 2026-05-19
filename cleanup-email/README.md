# cleanup-email

`cleanup-email` 是一个通过 IMAP 扫描并删除历史邮件的命令行脚本。主要用途是按配置中的规则批量清理旧邮件，同时对包含附件、回复、或满足重要发件人+标题匹配规则的邮件保留。

## 实现要点：

- 支持 Gmail / Yahoo（通过各自的 IMAP 服务）；需要使用 App Password 或服务端令牌。
- 支持多种密码解析方式（JSON 配置、macOS 钥匙串、~/.secrets 目录或环境变量）。
- 两个删除范围：`remove_all_before`（过期邮件删除所有非保护邮件）和 `remove_unimportant_before`（近期邮件仅删非重要邮件）。
- 设置 `start_date`, 忽略这一日期以前的邮件，在你再次运行时，可以忽略某日期以前的邮件以免重复处理
- 支持单日操作 `-d yyyy-mm-dd -m all`, 其中 `-m (--mode)`是处理模式，`all`将删除所有非保护邮件，`partial`仅删非重要邮件
- 交互确认与强制模式：默认交互（逐条确认），可用 `-f` 强制删除。
- 支持dry-run `--dry`, 打印信息但是不执行删除操作，便于调试

## 主要文件：

- `cleanup-email.py` — 主脚本

## 使用示例

### 交互确认模式（默认，每个删除询问用户）：

```bash
python3 cleanup-email.py -cfg xxjjs_ca.json
```

### 强制删除（不逐条询问）：

```bash
python3 cleanup-email.py -cfg xxjjs_ca.json -f
```

### 将运行日志输出到文件：

```bash
python3 cleanup-email.py -cfg xxjjs_ca.json -o cleanup.log
```

### 单日扫描，dryrun删除当天所有非重要文件, 观察信息并不执行删除操作：

```bash
python3 cleanup-email.py -cfg xxjjs_ca.json -d 2026-5-17 -m all -f --dry
```

## 配置说明（JSON 字段简介）

### 示例配置（摘录）：

```json
{
  "email_address": "you@example.com",
  "service_provider": "gmail",
  "auth": { "secret": "YOUR_APP_PASSWORD_HERE" },
  "remove_all_before": "2020-01-01",
  "remove_unimportant_before": "2022-01-01",
  "important_emails": [
    [".*@yourcompany\\.com", ""],
    ["service@paypal\\.com", ".*sent you .*USD"]
  ],
  "unimportant_email_subjects": ["newsletter", "promo"],
  "batch_size": 10,
  "start_date": null
}
```

### 关键说明：

- `auth.secret`：如果在配置中填写了有效的 App Password，会优先使用。否则脚本会尝试从 macOS 钥匙串、`~/.secrets` 目录或环境变量中解析密码。
- 请确保 IMAP 权限与 App Password 已启用（Gmail 的 App Password 或 Yahoo 的应用令牌）。
- 为防止超时，请设置恰当 `batch_size`, 即每次从邮箱中读取的邮件数量，缺省为10
- 建议首次运行请不要使用 `-f`，先用交互模式确认匹配结果，或使用`--dry` 

### `important_emails` 规则说明

- 格式：`important_emails` 是一个数组，内部元素为二元数组 `[sender_regex, subject_regex]`。
- 语义：发件人（`sender_regex`）和标题（`subject_regex`）必须同时匹配，才会将该邮件视为重要并受保护（即两者为 AND 关系）。
- 如果只想基于标题匹配，请将 `sender_regex` 设为 `.*` 或者空；反之若只想基于发件人匹配，将 `subject_regex` 设为 `.*`或者空。
- JSON 中的反斜杠需要进行转义，例如 `^service@paypal\.com$` 在 JSON 中应写为 `^service@paypal\\.com$`。

示例：要保留所有来自 PayPal 且标题形如 “Jamie sent you $3,400.00 USD” 的邮件，可以添加规则：

```json
["service@paypal\\.com", ".* sent you (?:\\$|€|¥|£|￥)?\\s*\\d{1,3}(?:,\\d{3})*(?:\\.\\d{2})?\\s*(?:[A-Z]{3})?"]
```

## 安全与注意事项

- 删除操作不可逆，请先备份重要邮件或先在小范围内测试。
- 对于公司或受管理的邮箱，请遵循公司合规与隐私策略。

## 进一步帮助

告诉我你的选择，我可以继续实现或添加示例配置。
