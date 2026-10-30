# 历史宣传素材

本目录保留 2026-10-16 的联合海报及原始截图，包含浏览器版和当时 HTTPS 版的旧行为。图中的 `nohup`、单场退出等说明不是当前使用步骤，`scanning` 也不代表进程健康或预约成功。当时的 HTTPS 版采用选听目标、systemd 运行说明及按活动 ID 的恢复保护，操作以 [README](../../README.md) 和[使用指南](../usage.md)为准；历史图像保持原样。

[高清 PNG](seu-lecture-poster.png) · [生成脚本](render_poster.py)

## 复现

在仓库根目录、已安装锁定依赖的环境执行：

```bash
uv run --locked python docs/promo/render_poster.py
```

命令覆盖同目录 PNG，不读取 `.env`、不联网、不访问学校或发信。脚本复用 Pillow、NumPy 和 OpenCV，不新增依赖；文案、颜色及两个公开仓库 URL 直接在脚本维护。

海报为 1800 × 3200 竖图。Windows 使用微软雅黑和 Consolas，Linux 使用 Noto Sans CJK 与 DejaVu Sans Mono；缺字体即报错，不下载或分发字体。修改后检查整图及缩小阅读效果，并确认两枚二维码和印刷链接指向对应仓库；脚本会在原尺寸及 900、720 像素预览中校验二维码。

## 截图来源

`assets/windows.png`、`assets/linux.png` 分别来自两个项目的 `docs/images/使用截图.png`，仅裁切标题栏、空白及重复提示符。用户已确认保留主机名和路径；原始截图不改动，复现不依赖另一份本地仓库。

历史素材不作为当前功能或真实预约验收证据。分享时建议发送原图，避免压缩损坏小字及二维码。
