# 微博博主发文云监控

基于 GitHub Actions 的微博博主发文定时监控系统。即使电脑关机，任务也会在 GitHub 云端按时执行，博文保存在仓库中。

## 功能特性

- **云端运行**：基于 GitHub Actions，不依赖本地电脑开机
- **定时抓取**：每 10 分钟自动检查博主的新博文
- **多源容错**：3 个 RSSHub 实例自动切换，一个挂了自动换下一个
- **增量保存**：通过链接对比，只保存新增博文，无新博文时不生成文件
- **永久存储**：博文以 Markdown 文件保存在仓库中，可直接在线浏览
- **公开仓库免费**：GitHub Actions 对公开仓库无限免费
- **零密钥配置**：使用 GitHub 内置 Token，无需手动创建 Personal Access Token

## 监控博主

| 博主 | UID | 微博主页 | 存储目录 |
|------|-----|----------|----------|
| 岚论 | 1657450041 | https://weibo.com/u/1657450041 | `weibo_岚论/` |
| 菩提树下那道光 | 1002568141 | https://weibo.com/u/1002568141 | `weibo_菩提树下那道光/` |

## 部署步骤

### 1. Fork 或推送代码到 GitHub

- 创建一个 **Public** 仓库
- 推送代码：
```bash
git init
git add .
git commit -m "初始化微博监控项目"
git branch -M main
git remote add origin https://github.com/你的用户名/weibo.git
git push -u origin main
```

### 2. 启用 Actions

- 进入仓库页面 → **Actions** 标签
- 点击 **I understand my workflows, go ahead and enable them**
- 手动触发一次：点击左侧 **微博博主发文监控** → **Run workflow** → **Run workflow**

> 无需配置 Token 或 Secrets —— GitHub Actions 自动提供内置 Token 用于提交推送。

## 项目结构

```
weibo/
├── .github/workflows/
│   └── weibo-monitor.yml      # GitHub Actions 工作流
├── scripts/
│   └── weibo_monitor.py       # Python 监控脚本
├── weibo_岚论/                 # 博主「岚论」发文记录
├── weibo_菩提树下那道光/         # 博主「菩提树下那道光」发文记录
├── requirements.txt
└── README.md
```

## 自定义配置

### 添加更多博主

编辑 `scripts/weibo_monitor.py`，在 `BLOGGERS` 列表中添加：

```python
{
    "name": "博主昵称",
    "uid": "微博UID",
    "url": "https://weibo.com/u/微博UID",
    "dir": "weibo_博主昵称"
},
```

### 修改抓取频率

编辑 `.github/workflows/weibo-monitor.yml` 中的 cron：

```yaml
on:
  schedule:
    - cron: '*/10 * * * *'  # 每10分钟（当前）
    - cron: '*/30 * * * *'  # 每30分钟
    - cron: '0 * * * *'     # 每小时
```

### 更换 RSSHub 实例

编辑 `scripts/weibo_monitor.py` 中的 `RSSHUB_INSTANCES` 列表即可。

## 技术说明

- **数据来源**：RSSHub 开源订阅源，无需登录微博
- **新增检测**：通过对比已有博文链接判断是否新增
- **免费额度**：公开仓库 GitHub Actions 完全免费，无时间限制
- **推送机制**：使用 GitHub Actions 内置 `GITHUB_TOKEN`，自动具备仓库写权限

## 常见问题

**Q: Action 没有按时运行？**
A: GitHub Actions 定时任务可能有 5-15 分钟延迟，属正常现象。

**Q: RSSHub 无法访问？**
A: 脚本内置 3 个备用实例自动切换，一般不受影响。

**Q: 如何查看抓取日志？**
A: 仓库 → Actions → 点击某次运行 → 展开各步骤查看详细输出。
