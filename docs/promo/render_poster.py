"""Render the agreed 1800 × 3200 poster, using only local screenshot assets."""

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "seu-lecture-poster.png"
WIDTH, HEIGHT = 1800, 3200
CREAM = "#FBDBAF"
MUTED = "#C4AC90"
ORANGE = "#F17A32"
WINDOWS_URL = "https://github.com/MuZiJin701/SEU_Lecture_Reserve"
LINUX_URL = "https://github.com/MuZiJin701/seu-lecture-reserve-http"


@lru_cache
def font(size: int, bold: bool = False, latin: bool = False):
    candidates = (
        ["C:/Windows/Fonts/consolab.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"]
        if latin
        else [
            "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold
            else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
    )
    for path in candidates:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise FileNotFoundError("需要微软雅黑或 Noto Sans CJK 中文字体；英文字体使用 Consolas 或 DejaVu Sans Mono。")


def text(draw, xy, value, size, fill=CREAM, *, bold=False, latin=False, max_width=None):
    face = font(size, bold, latin)
    if max_width is not None:
        assert draw.textlength(value, font=face) <= max_width, f"文字超宽：{value}"
    draw.text(xy, value, font=face, fill=fill, anchor="lt")


def screenshot(canvas, filename, box):
    source = Image.open(ROOT / "assets" / filename).convert("RGB")
    fitted = ImageOps.contain(source, (box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS)
    x = box[0] + (box[2] - box[0] - fitted.width) // 2
    y = box[1] + (box[3] - box[1] - fitted.height) // 2
    canvas.paste(fitted, (x, y))


def terminal(canvas, y, filename, label, height):
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((150, y, 1650, y + height), radius=20, fill="#151815", outline="#4D5140", width=2)
    for x, color in [(182, "#D98459"), (211, "#D9B16F"), (240, "#899A6D")]:
        draw.ellipse((x, y + 20, x + 13, y + 33), fill=color)
    text(draw, (287, y + 17), label, 23, MUTED, latin=True)
    screenshot(canvas, filename, (166, y + 58, 1634, y + height - 19))


def card(canvas, y, number, platform, project, features, filename, terminal_label, caption, extra=0):
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((100, y, 1700, y + 754 + extra), radius=32, fill="#221C17", outline="#6B4931", width=2)
    draw.rounded_rectangle((100, y, 111, y + 754 + extra), radius=5, fill=ORANGE)
    text(draw, (153, y + 45), number, 66, ORANGE, latin=True)
    text(draw, (281, y + 49), platform, 50, bold=True, max_width=1320)
    text(draw, (284, y + 121), project, 30, MUTED, latin=True, max_width=1300)
    draw.line((150, y + 182, 1650, y + 182), fill="#62452F", width=2)
    for index, feature in enumerate(features):
        top = y + 205 + index * 52
        draw.polygon([(155, top + 6), (168, top + 17), (155, top + 28)], fill=ORANGE)
        text(draw, (190, top), feature, 35, max_width=1430)
    terminal(canvas, y + 357, filename, terminal_label, 360 + extra)
    text(draw, (156, y + 726 + extra), caption, 19, MUTED, max_width=1470)


def qr(canvas, url, xy):
    params = cv2.QRCodeEncoder_Params()
    params.correction_level = cv2.QRCodeEncoder_CORRECT_LEVEL_Q
    modules = Image.fromarray(cv2.QRCodeEncoder_create(params).encode(url))
    modules = ImageOps.expand(modules, border=2, fill=255)
    scale = 280 // modules.width
    assert scale >= 4
    code = modules.resize((modules.width * scale, modules.height * scale), Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(canvas)
    x, y = xy
    draw.rounded_rectangle((x, y, x + 304, y + 304), radius=18, fill="#FFFFFF")
    canvas.paste(code, (x + (304 - code.width) // 2, y + (304 - code.height) // 2))
    crop = np.asarray(canvas.crop((x, y, x + 304, y + 304)))
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(crop)
    assert decoded == url, f"二维码校验失败：{url}"


def main():
    canvas = Image.new("RGB", (WIDTH, HEIGHT), "#15110F")
    draw = ImageDraw.Draw(canvas)
    for y in range(HEIGHT):
        strength = max(0, 1 - y / 1000)
        draw.line((0, y, WIDTH, y), fill=(int(24 + 17 * strength), int(19 + 4 * strength), 15))

    glow = Image.new("RGBA", canvas.size)
    light = ImageDraw.Draw(glow)
    light.ellipse((1000, -460, 2260, 830), fill=(225, 93, 21, 115))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(130))).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    for radius in (290, 385, 480, 615):
        draw.ellipse((1640 - radius, 260 - radius, 1640 + radius, 260 + radius), outline="#785031", width=2)
    for offset in (0, 42, 84):
        draw.line((1340 + offset, 0, 1000 + offset, 700), fill="#473022", width=2)
    draw.polygon([(1737, 740), (1800, 680), (1800, 2590), (1737, 2650)], fill="#AF4E20")
    draw.line((100, 104, 1700, 104), fill="#A7693D", width=2)
    text(draw, (103, 135), "SEU // LECTURE RESERVATION", 29, ORANGE, latin=True)
    text(draw, (1431, 135), "VOL. 01 / 02", 26, MUTED, latin=True)
    text(draw, (100, 233), "东南大学", 92, bold=True)
    text(draw, (91, 363), "素养讲座预约脚本", 165, bold=True, max_width=1600)
    text(draw, (108, 579), "本机运行，或交给云端。", 49)
    text(draw, (108, 665), "PYTHON  /  WINDOWS + CHROME  /  LINUX + HTTPS", 26, ORANGE, latin=True)
    draw.line((100, 735, 1700, 735), fill="#6E4931", width=2)

    card(
        canvas, 765, "01", "WINDOWS / 本机浏览器", "SEU_Lecture_Reserve",
        ["Chrome 浏览器运行 · 无需 ChromeDriver", "持续扫描，识别图形验证码并尝试预约", "确认预约一场后退出"],
        "windows.png", "POWERSHELL // SCANNING", "真实终端截图 · 保留原始命令与输出 · 扫描状态不代表预约成功", extra=96,
    )
    card(
        canvas, 1650, "02", "LINUX / 云服务器", "seu-lecture-reserve-http",
        ["无需浏览器 · 兼容 Windows", "按选听目标持续查找讲座", "后台运行 · 可选邮件通知 · 停止后可继续"],
        "linux.png", "HTTPS CLIENT // LOCAL STATUS", "真实终端截图 · 保留原始命令与输出 · scanning 仅为本地任务状态",
    )

    draw = ImageDraw.Draw(canvas)
    text(draw, (105, 2448), "选择运行方式，扫码查看源码与使用说明", 43, bold=True)
    text(draw, (108, 2519), "OPEN SOURCE / TWO WAYS TO RUN", 25, ORANGE, latin=True)
    qr(canvas, WINDOWS_URL, (110, 2590))
    qr(canvas, LINUX_URL, (935, 2590))
    text(draw, (450, 2613), "01 / WINDOWS", 29, ORANGE, latin=True)
    text(draw, (450, 2674), "浏览器版", 44, bold=True)
    text(draw, (450, 2750), "SEU_Lecture", 25, MUTED, latin=True)
    text(draw, (450, 2791), "_Reserve", 25, MUTED, latin=True)
    text(draw, (1275, 2613), "02 / LINUX", 29, ORANGE, latin=True)
    text(draw, (1275, 2674), "HTTPS 版", 44, bold=True)
    text(draw, (1275, 2750), "seu-lecture-", 25, MUTED, latin=True)
    text(draw, (1275, 2791), "reserve-http", 25, MUTED, latin=True)
    text(draw, (110, 2909), WINDOWS_URL, 24, MUTED, latin=True, max_width=765)
    text(draw, (935, 2909), LINUX_URL, 24, MUTED, latin=True, max_width=765)
    draw.line((100, 2954, 1700, 2954), fill="#6E4931", width=2)
    text(draw, (108, 2980), "短信认证需人工处理；预约结果以学校系统为准。", 30, MUTED, max_width=1580)
    text(draw, (108, 3035), "服务器版真实预约及长期运行仍待验收。", 30, MUTED, max_width=1580)
    text(draw, (108, 3120), "MuZiJin701 / 个人开源项目", 27, ORANGE)
    text(draw, (1422, 3120), "SEU // 2026", 27, MUTED, latin=True)

    assert canvas.size == (1800, 3200)
    canvas.save(OUTPUT, optimize=True)
    with Image.open(OUTPUT) as saved:
        saved.verify()
    for width in (900, 720):
        factor = width / WIDTH
        preview = canvas.resize((width, round(HEIGHT * factor)), Image.Resampling.LANCZOS)
        for x, url in ((110, WINDOWS_URL), (935, LINUX_URL)):
            box = tuple(round(v * factor) for v in (x, 2590, x + 304, 2894))
            decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(np.asarray(preview.crop(box)))
            assert decoded == url, f"缩至 {width}px 后二维码校验失败：{url}"
    print(f"Generated {OUTPUT} ({WIDTH} x {HEIGHT}); both QR codes verified at 1800, 900 and 720px widths.")


if __name__ == "__main__":
    main()
