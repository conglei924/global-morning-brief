# 全球晨报

每天从明确的权威媒体白名单读取 RSS，去掉重复报道，再将带原文链接的晨报通过云端邮件 API 发到邮箱。它不使用“全网热搜”或来源不明的聚合站；每一项都保留媒体名称和原始链接，方便核验。

## 已纳入的项目思路

- [Miniflux](https://github.com/miniflux/v2)：成熟的 RSS 阅读器，适合想要一个长期浏览历史的网页服务时使用。
- [FreshRSS](https://github.com/FreshRSS/FreshRSS)：带网页界面和抓取扩展的完整 RSS 聚合器。
- 本项目采用更轻量的方案：直接抓取媒体自己提供的 RSS，适合 Windows 本机每天邮件推送。

## 安装与首次预览

在 PowerShell 中运行：

```powershell
cd 'C:\Users\54376\Documents\新闻简报'
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
.\.venv\Scripts\python morning_brief.py --dry-run
```

如果 `.venv` 中提示没有 `pip`（当前这台电脑的 Python 会出现这个情况），改用下面一行安装依赖：

```powershell
python -m pip --python .\.venv\Scripts\python.exe install -r requirements.txt
```

当前收件人已设为 `conglei824@gmail.com`。若同时填写 `OPENAI_API_KEY` 与 `OPENAI_MODEL`，邮件正文会生成中文摘要；不填时，仍会发送可核验的英文原始头条。

## 首次 Gmail OAuth 授权

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 新建项目，并在“API 和服务”启用 **Gmail API**。
2. 打开 **Google Auth platform**：按向导完成 Branding / Audience / Data Access；个人 Gmail 请选择 External，并将 `conglei924@gmail.com` 加到测试用户。
3. 在 **Clients** 创建 OAuth 客户端，类型选 **Desktop app**；下载 JSON，命名为 `credentials.json` 并放入本目录。该文件和授权令牌已被 `.gitignore` 排除。
4. 运行一次授权：

```powershell
.\.venv\Scripts\python morning_brief.py --authorize
```

浏览器会要求登录 `conglei924@gmail.com` 并允许“全球晨报”发送邮件。完成后会生成仅本机保存的 `data/gmail-token.json`；定时任务会自动刷新它，无需再次登录。

预览会生成 `preview.html`。浏览器确认内容后，手动发送一次：

```powershell
.\.venv\Scripts\python morning_brief.py
```

## 云端每日定时（推荐）

推送由 GitHub Actions 在每天 **08:30（北京时间）**运行，电脑关机也不受影响。将该目录推送到一个 GitHub 仓库后，在仓库 Settings → Secrets and variables → Actions 添加：

- `RESEND_API_KEY`：Resend API 密钥。
- `EMAIL_FROM`：Resend 已验证的发件人，例如 `全球晨报 <news@你的域名>`。
- `EMAIL_TO`：`465257249@qq.com`。
- `OPENAI_API_KEY`：用于把原始报道压缩为中文热点摘要。
- `OPENAI_MODEL`：你可用的 OpenAI 模型名称。

工作流在 `.github/workflows/morning-brief.yml`；也可以在 Actions 页面手动运行测试。邮件默认只发送 6–10 条中文热点摘要，原文链接仅在末尾作核验备用。Resend 测试发件地址只能发给已验证的收件人，向任意地址发送前需要验证一个自己的域名。

## 本机每日定时（仅作备用）

确认测试邮件后，创建 Windows 任务计划（默认 07:30）：

```powershell
.\install_schedule.ps1 -Time '07:30'
```

删除任务：`Unregister-ScheduledTask -TaskName 'GlobalMorningBrief' -Confirm:$false`

## 维护可信来源

在 `sources.json` 增删媒体 RSS。当前白名单是 BBC、德国之声、France 24、卫报、NPR 和半岛电视台，覆盖多地区与不同编辑体系。来源的“权威性”是编辑政策判断，不代表任何报道绝对无误；本工具通过来源透明和原文链接保留核验路径。
