# 联合宣传海报

[查看高清 PNG](seu-lecture-poster.png) · [生成脚本](render_poster.py)

海报标题为“东南大学素养讲座预约脚本”，采用 1800 × 3200 像素竖版，适合手机传播。使用橙黑底色、奶油色标题、技术标签和卡片布局，参考用户提供的 Cyber Ronin 模板风格；不使用模板的远程图片、字体或网页交互。两枚项目二维码下方分别印有对应仓库的完整 HTTPS 链接，与二维码复用相同的 URL。

两个项目按各自文档定位展示：

- Windows 浏览器版：[SEU_Lecture_Reserve](https://github.com/MuZiJin701/SEU_Lecture_Reserve)，使用 Chrome，持续扫描，确认一场预约后退出。
- Linux 服务器版：[seu-lecture-reserve-http](https://github.com/MuZiJin701/seu-lecture-reserve-http)，无需浏览器，按选听目标持续查找，支持后台运行和可选邮件通知，也兼容 Windows。

## 重新生成

在当前仓库根目录，使用已按[使用指南](../usage.md)准备好的项目环境运行：

```sh
uv run --locked python docs/promo/render_poster.py
```

命令覆盖同目录的 `seu-lecture-poster.png`。不读取 `.env`、不访问网络、不登录学校或提交预约，也不发送邮件。

脚本复用锁文件中已有的 Pillow、NumPy 和 OpenCV，不新增依赖。中文字体优先使用 Windows 微软雅黑；Linux 使用 `/usr/share/fonts/opentype/noto/` 下的 Noto Sans CJK。英文字体使用 Consolas 或 DejaVu Sans Mono。缺少字体时明确报错，不下载字体，也不将系统字体打包分发。不同平台的字体可能造成少量排版差异，重新生成后需目视检查。

生成时检查图片尺寸、关键文案宽度和 PNG 完整性，并在原尺寸以及缩至 900、720 像素宽的预览中分别解码校验两枚二维码指向对应仓库。二维码直接在本地生成，不依赖第三方短链。

## 截图与公开范围

- `assets/windows.png` 来自浏览器版仓库的 `docs/images/使用截图.png`，裁去标题栏与下方空白，保留命令、项目说明、登录及扫描输出。
- `assets/linux.png` 来自当前仓库的 `docs/images/使用截图.png`，裁去标题栏、底部重复提示符及空白，保留原始命令及状态输出。
- 用户已明确确认主机名与路径不需要屏蔽，素材保留这些原始内容。原始截图不改动；重新生成只需要此目录内的裁切素材，不依赖另一个本地仓库。
- 截图里的扫描状态不代表进程健康或预约成功。海报保留人工短信认证和学校系统结果核对提示；服务器版真实预约及长期运行仍待验收。

## 修改与验收

文案、颜色、位置和两个公开仓库 URL 均在脚本中直接维护。调整文案或二维码目标后重新生成，检查完整海报和缩小后的阅读效果，再用手机核对二维码。通过聊天软件转发时建议发送原图，避免压缩影响小字与二维码。

这是宣传素材，不改变预约逻辑或领域定义；视觉选择可随时调整，不新增 ADR 或术语。
