# MoviePilot-Plugins
MoviePilot官方插件市场：https://github.com/jxxghp/MoviePilot-Plugins

### [媒体库封面生成](https://github.com/justzerock/MoviePilot-Plugins/tree/main/plugins.v2/mediacovergenerator)
  > 参考项目：https://github.com/HappyQuQu/jellyfin-library-poster

  在群里受到这个项目的启发，督促 AI 帮我写封面处理的代码，于是有了这个插件，支持切换风格

  ![插件界面](https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/images/plugin.webp)

### [每日入库速报](https://github.com/kongchengll/MoviePilot-Plugins/tree/main/plugins.v3/dailyepisodedigest)
  > MoviePilot V3 插件

  定时统计每天新入库的剧集与电影，把每部作品的封面、标题、季集与入库时间渲染成一张
  1080 宽的日报长图，通过 MoviePilot 自身的通知渠道推送。

  支持自定义 cron、三种统计窗口（昨日 / 今日 / 最近 24 小时）、电视剧与电影分别筛选、
  排除关键词；也可在消息渠道发送 `/digest_push` 随时手动触发一次。

  详见 [插件说明](plugins.v3/dailyepisodedigest/README.md)。

### [下载监控](https://github.com/kongchengll/MoviePilot-Plugins/tree/main/plugins.v3/downloadwatch)
  > MoviePilot V3 插件

  定时扫描下载任务，两条规则任一命中即清理：「等待 ≥ X 小时且进度 ≤ Y%」的卡死任务，
  以及监控满 24 小时仍未下载完成的任务（默认连本地文件一起删）。支持按下载器标签
  筛选、演练模式、规则二独立的删除文件开关。

  删除任务后会按下载历史回滚对应订阅的「已下载进度」，让订阅重新搜索这一集（否则宿主会
  认为该集已经下过、永远跳过）；种子同时进入本地黑名单，再次被下载时当轮清理。也可以
  选择把种子标题追加进订阅排除规则，从源头阻止重复下载。

  任务进入监控与被删除时均可推送通知，插件详情页可直接查看「监控中」「已删除」与
  「黑名单」列表，黑名单条目支持逐条移除。

  详见 [插件说明](plugins.v3/downloadwatch/README.md)。