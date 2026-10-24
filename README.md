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