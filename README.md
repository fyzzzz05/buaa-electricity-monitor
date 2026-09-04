# 北航宿舍电费监控

这是一个可以直接放进 GitHub 仓库的轻量监控项目。它每天分别检查：

- 空调电表：`44229`，地址必须包含 `[空调]`
- 照明电表：`44588`，地址必须包含 `[照明]`

当前学校页面展示的是“截止当天 00:00”的数据，因此默认每天北京时间 **08:17** 查询一次。频繁到每小时不会获得更实时的数据，反而会增加不必要的请求。

## 它如何工作

1. GitHub Actions 按时启动一台临时 Linux runner。
2. `monitor.py` 请求两个学校查询页面。
3. 脚本从 HTML 的 `#canvas1` 提取剩余电量，并校验电表号与地址类型。
4. 两个电表使用各自的阈值判断，结果写入本次 Actions 的 Summary。
5. 任一电表低于阈值，或者接口异常时，通过 Telegram 发消息。

项目只使用 Python 标准库，不需要 `requirements.txt`，也不需要保存学校登录 Cookie。

## 第一步：先在本地验证

进入项目目录后执行：

```bash
python3 -m unittest discover -s tests -v
python3 monitor.py --no-notify
```

第二条命令会访问真实接口，但不会发送 Telegram。查询结果会显示在终端，并保存为 `monitor-result.json`。

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

如果测试消息和低电量告警都能收到，部署就完成了。

## 调整阈值

在 `config.json` 中分别修改：

```json
"warning_threshold_kwh": 30,
"critical_threshold_kwh": 15
```

空调和照明的阈值互不影响。由于学校数据按天更新，阈值应留出至少一天的正常用电余量。

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
- 当前版本不保存跨运行的累计历史；每次 Actions Run 的 Summary 就是一条可追溯记录。
