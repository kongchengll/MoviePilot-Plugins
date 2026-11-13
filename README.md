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

  删除任务后默认执行「换源」：先把种子标题写进对应订阅的排除正则，让宿主在搜索阶段
  就过滤掉它，只能去找其它站点/其它发布组的同集种子；再把这一集从订阅的「已下载进度」
  里撤回（否则宿主会认为该集已经下过、永远跳过）。只撤回不排除会让宿主反复重下同一个
  「最优」种子，形成「删了又下、下了又删」的死循环，所以两者顺序固定、且排除在前。
  种子同时进入本地黑名单，再次被下载时当轮清理；同集换源次数超过上限（默认 5 次）后
  停止换源并保留「已下载」标记，交给人工处理。

  任务进入监控与被删除时均可推送通知，插件详情页可直接查看「监控中」「已删除」与
  「黑名单」列表，黑名单条目支持逐条移除——移除时会连带撤销它写进订阅的排除规则，
  该种子重新可选、该集的换源次数也会清零。

  详见 [插件说明](plugins.v3/downloadwatch/README.md)。