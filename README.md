# 平静的个人工作台

一封邮件问教室定了没有。你可以先把它收好，继续校对报名页。准备给这件事留时间时，再把它提为今天的三件承诺之一。这个本地工作台把收件箱、日程、项目卡、收工日志和周报接在一起。

## 打开就能试

[下载单文件演示版](https://github.com/anpingjing/calm-workbench/releases/latest/download/calm-workbench.html)，用桌面浏览器打开。也可以从仓库下载 ZIP，解压后打开根目录的 `index.html`。演示使用虚构的读书会资料，无需安装依赖或连接个人账户。

1. 在「今日」找到“确认读书会教室”，点「收进今天」。它会进入「其余」。
2. 点「提为承诺」，填入 20 分钟。「今日时段」会避开示例里的十点碰头会。
3. 到「收工」勾掉完成项，给剩下的选“等待确认”，写一句明早从哪里开始。可下载日志、结转未完成事项。
4. 到「项目」看卡点和下一步。在「周」填写下周重点，下载周报草稿并补上本周结论。

编辑状态保存在当前浏览器中。更换设备或浏览器前，可在「系统」导出状态备份。

## 换成自己的项目

在解压后的仓库根目录，用 Python 3 运行以下命令。下例为 macOS / Linux。Windows 用户也可以用文件管理器创建对应目录，复制文件后执行构建脚本。

```sh
mkdir -p 01-项目 02-产出 03-数据 local/logs
cp examples/读书会.md 01-项目/
cp examples/inbox.json 03-数据/
python3 04-自动化/build_workbench.py
```

打开新生成的 `工作台.html`。编辑 `01-项目/` 下的 Markdown 项目卡，或向 `03-数据/inbox.json` 增加请求，再运行构建命令刷新。把页面下载的日终日志放入 `local/logs/`，下次构建会按日期收进 `00-今日/`，供周回顾使用。个人项目、收件箱、日志和生成的个人页面均已加入 Git 忽略清单。

## 源码与可选接入

- `04-自动化/workbench_template.html` 包含页面、任务选择、估时排程、结转和周报导出的逻辑。
- `04-自动化/build_workbench.py` 读取本地项目卡、日志、产出和来源快照，生成个人工作台。核心构建只使用 Python 标准库。
- `examples/demo.json` 是公开演示数据。执行 `python3 examples/render_demo.py` 可重建演示首页。
- `collect_calendar.py` 和 `collect_feishu.py` 依赖本机已登录并获授权的 lark-cli。飞书应用 ID 可通过 `LARK_APP_ID` 配置。
- `collect_email.py` 依赖已配置的 Himalaya CLI，以及 `HIMALAYA_ACCOUNT` 和 `WORKBENCH_EMAIL`。微信候选脚本读取使用者已有的 `local/merged.ndjson`。
- `collect_reports.py` 是针对特定报表页面结构的适配示例，需自行配置 `local/report_sources.json`、Playwright 和 Chrome。个人账号授权、实际采集与定时运行由使用者自行配置。
