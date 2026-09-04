# 北航宿舍电费监控

这是一个可以直接放进 GitHub 仓库的轻量监控项目。它每天分别检查：

- 空调电表：`xxxxx`，地址必须包含 `[空调]`
- 照明电表：`xxxxx`，地址必须包含 `[照明]`

当前学校页面展示的是“截止当天 00:00”的数据，因此默认每天北京时间 **08:17** 查询一次。频繁到每小时不会获得更实时的数据，反而会增加不必要的请求。

## 它如何工作

1. GitHub Actions 按时启动一台临时 Linux runner。
2. `monitor.py` 请求两个学校查询页面。
3. 脚本从 HTML 的 `#canvas1` 提取剩余电量，并校验电表号与地址类型。
4. 把每日结果写入 `data/history.csv`，并生成最近 30 天折线图。
5. Telegram 每天发送完整日报图片，同时标出低于 ¥10 告警线的电表。
6. GitHub Actions 把更新后的 CSV 自动提交回仓库，供下一次绘图使用。

学校页面请求和 Telegram 通知只使用 Python 标准库；折线图使用
`matplotlib`，由 GitHub Actions 根据 `requirements.txt` 自动安装。项目不需要保存学校登录 Cookie。

## 第一步：先在本地验证

进入项目目录后执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 monitor.py --no-notify
```

最后一条命令会访问真实接口，但不会发送 Telegram。查询结果会显示在终端，保存为
`monitor-result.json`，并生成 `electricity-history.png`。

## 第二步：创建 Telegram Bot

1. 在 Telegram 中打开 `@BotFather`。
2. 发送 `/newbot`，按提示创建机器人，保存得到的 Bot Token。
3. 打开刚创建的机器人，至少发送一条消息，例如 `hello`。
4. 在终端安全地输入 Token 并查询更新：

```bash
read -s TELEGRAM_BOT_TOKEN
echo
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"
unset TELEGRAM_BOT_TOKEN
```

返回 JSON 中 `message.chat.id` 的数字就是 Chat ID。如果机器人发到群组，应先把机器人加入群组并在群里发一条消息；群组 Chat ID 通常是负数。

不要把 Token 写入 `config.json`，也不要提交到 Git。

## 第三步：创建 GitHub 仓库

建议使用私有仓库，因为配置中包含宿舍电表号。

在 GitHub 新建一个空仓库，然后在本项目目录执行：

```bash
git init
git add .
git commit -m "Add dorm electricity monitor"
git branch -M main
git remote add origin 你的仓库地址
git push -u origin main
```

确认 `.github/workflows/electricity-monitor.yml` 也被提交了。定时工作流必须位于默认分支。

## 第四步：配置 GitHub Actions Secrets

打开仓库：

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

添加两个 Secret：

| 名称 | 内容 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 提供的 Token |
| `TELEGRAM_CHAT_ID` | 你或宿舍群的 Chat ID |

Secrets 会在工作流中通过环境变量注入，脚本不会打印它们。

## 第五步：手动执行第一次监控

1. 打开仓库的 `Actions` 标签页。
2. 左侧选择 `Dorm electricity monitor`。
3. 点击 `Run workflow`。
4. 第一次建议勾选 `Send a Telegram test message...`。
5. 运行结束后打开该次运行，在页面下方查看 `宿舍电费监控结果` Summary。

如果测试消息以及带折线图的完整日报都能收到，部署就完成了。从第二天开始，图中会逐日形成折线。

## 调整阈值

在 `config.json` 中分别修改金额阈值：

```json
"alert_threshold_cny": 10
```

脚本用页面电价把剩余 kWh 折算成人民币。当前两块表都在剩余价值不高于 ¥10 时告警；空调和照明仍可设置不同阈值。

## 历史与折线图

- `data/history.csv` 每个数据日期、每块电表只保留一行，手动重复运行不会产生重复点。
- 图表展示最近 30 天，空调与照明分为上下两张子图，避免量程互相影响。
- 红色虚线表示折算后的 ¥10 告警线。
- PNG 只在 runner 中临时生成并发送，不提交进 Git，避免仓库体积持续增长。
- 工作流需要 `contents: write` 权限，仅用于提交 `data/history.csv`。

## 调整执行时间

工作流目前使用 GitHub Actions 的时区写法：

```yaml
schedule:
  - cron: "17 8 * * *"
    timezone: "Asia/Shanghai"
```

含义是每天北京时间 08:17。保留非整点分钟可以降低 GitHub Actions 高峰期延迟概率。

还可以随时通过 `workflow_dispatch` 在 Actions 页面手动运行，不必等待定时任务。

## 需要知道的限制

- 学校页面明确说明数据可能与实际值存在偏差，告警后仍应人工复核。
- GitHub 定时任务不是实时系统，高负载时可能延迟，极端情况下可能漏掉一次执行。
- 定时工作流只在默认分支执行。
- 公共仓库连续 60 天没有活动时，GitHub 会自动停用 scheduled workflow；使用私有仓库或定期关注运行状态更稳妥。
- 如果仓库规则禁止 `GITHUB_TOKEN` 向默认分支推送，日报仍会发送，但历史提交步骤会失败；需要在仓库规则中允许 GitHub Actions 写入，或改用单独的数据分支。
