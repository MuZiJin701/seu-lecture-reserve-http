# 历史宣传素材

本目录保存 **2026-10-16 的旧海报**，不是当前使用教程。图中的 HTTPS 管理台、`nohup`、单场退出或旧统计方式可能已经变化；`scanning` 也不能证明预约成功。现在的安装和操作请看 [README](../../README.md) 与[使用指南](../usage.md)。

[查看高清海报](seu-lecture-poster.png) · [生成脚本](render_poster.py)

## 重新生成海报

安装项目锁定依赖后，在仓库根目录执行：

```bash
uv run --locked python docs/promo/render_poster.py
```

命令会覆盖本目录的 PNG，只读取本地截图，不加载 `.env`、不联网，也不访问学校或发送邮件。它使用已有的 Pillow、NumPy 和 OpenCV；文案、颜色和两个公开仓库地址在脚本中维护。

输出为 1800 × 3200 竖图。Windows 使用微软雅黑、Consolas，Linux 使用 Noto Sans CJK、DejaVu Sans Mono；缺字体时会报错，不自动下载。脚本会在原图及 900、720 像素预览中检查二维码，生成后还需检查小字、整图和链接是否清楚。

## 截图来源与分享

`assets/windows.png`、`assets/linux.png` 分别来自两个项目的 `docs/images/使用截图.png`，只裁掉标题栏、空白和重复提示符。用户已确认保留主机名和路径；原始截图不改动，重新生成也无需另一个本地仓库。

海报只用于回看历史，不能证明当前功能或预约效果。分享时发送原图，避免压缩损坏小字和二维码。
