# -*- coding: utf-8 -*-
"""德州扑克 · Windows XP 风格图形启动器（PyQt5）。

双击「PokerLauncher.exe」或「启动器.lnk」打开：选机器人数、勾选哪些座位启用
在线模型、填 API 配置，点「启动游戏」即在新控制台窗口里运行 play.ps1。
源码模式用 .venv 里的 PyQt5；PyInstaller 冻结后 Qt 已打进 exe，零外部依赖。
exe 只带界面，游戏本体（poker.py / play.ps1 / poker-config.bat）仍在 exe 同目录。
Key 只通过环境变量传给子进程，不写日志、不回显。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import traceback

# PyInstaller 冻结后 __file__ 指向临时解压目录（_MEIxxxx），
# 所有「同目录」资源必须以 exe 所在目录为基准，否则找不到 play.ps1 / poker-config.bat。
IS_FROZEN = getattr(sys, "frozen", False)
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable))
          if IS_FROZEN else SRC_DIR)

# 源码模式：无论双击时 Windows 把 .pyw 交给哪个 Python，都自动切到项目内带 PyQt5 的解释器。
# 冻结模式：Qt 已经打进 exe，不需要也不应该再自举。
if not IS_FROZEN:
    _VENV_PYW = os.path.join(APP_DIR, ".venv", "Scripts", "pythonw.exe")
    if (os.path.normcase(sys.executable) != os.path.normcase(_VENV_PYW)
            and os.path.isfile(_VENV_PYW)):
        try:
            import PyQt5 as _qt_check  # noqa: F401
        except ImportError:
            subprocess.Popen([_VENV_PYW, os.path.abspath(__file__)], cwd=APP_DIR)
            sys.exit(0)

try:
    from PyQt5.QtCore import (QEvent, QPointF, QRect, QRectF, Qt, QThread,
                              QTimer, pyqtSignal)
    from PyQt5.QtGui import (QBrush, QColor, QConicalGradient, QFont,
                             QFontDatabase, QLinearGradient, QPainter,
                             QPainterPath, QPalette, QPen, QPixmap,
                             QRadialGradient, QRegion)
    from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                                 QFrame, QGroupBox,
                                 QHBoxLayout, QLabel, QLineEdit, QMenuBar,
                                 QPlainTextEdit, QPushButton, QSizePolicy,
                                 QSpinBox, QVBoxLayout, QWidget)
except ImportError:
    import ctypes
    ctypes.windll.user32.MessageBoxW(
        0, "启动器需要 PyQt5。请先在项目目录运行：\n"
           "uv pip install --python .venv\\Scripts\\python.exe PyQt5",
        "德州扑克启动器", 0x10)
    raise

HERE = APP_DIR
PLAY_PS1 = os.path.join(HERE, "play.ps1")
GAME_EXE = os.path.join(HERE, "PokerGame.exe")   # 分享包里优先用它，对方不需要 Python
CONFIG_BAT = os.path.join(HERE, "poker-config.bat")
ERROR_LOG = os.path.join(HERE, "launcher-error.log")

PROVIDERS = [("自动识别", "auto"),
             ("TypeSafe Jev（决策模型 /v1/systemone）", "jev"),
             ("OpenAI 兼容（聊天模型 /chat/completions）", "openai")]
CARD_STYLES = [("中文花色  方9 红A", "cn"),
               ("英文缩写  9d / As", "ascii"),
               ("花色符号  9♦ / A♠", "pretty")]
# 淘汰赛排在第一位 = 默认；现金局留给「想一直打下去、筹码见底能补」的场合
MODES = [("淘汰赛（见底出局）", "tournament"),
         ("现金局（可补筹码）", "cash")]
# 与 deepseek.PERSONAS 的 tag 一一对应（test_poker 里有断言，防止两边走偏）
# 默认就是「随机」= 开局从风格池里抽，一桌尽量不重样；重开一场时会重新抽。
STYLE_TAGS = ["随机", "稳健", "激进", "紧凶", "松凶", "跟注站"]
STYLE_LABELS = ["随机", "稳健", "激进", "紧凶", "松凶", "跟注站"]
SEAT_FOLLOW = "跟随全局"
# DeepSeek 官网的模型名（含已下线的旧名）：填进 Jev 配置时一律清掉，
# 否则会把 Jev key 发到聊天网关。
DEEPSEEK_MODELS = ("deepseek-chat", "deepseek-reasoner", "deepseek-flash",
                   "deepseek-v4-flash", "deepseek-v4-pro")

KEY_PREFIXES = ("apikey_", "tsf_", "sk-", "sk_")
try:
    from memory import WINDOW_DEFAULT as MEMORY_WINDOW_DEFAULT
except Exception:            # 极端情况下（文件缺失）也不该让界面起不来
    MEMORY_WINDOW_DEFAULT = 10


def looks_like_api_key(value: str) -> bool:
    """用户常把 Key 粘进「API 地址」框；这种值不能拿去拼 URL。"""
    v = (value or "").strip()
    if not v:
        return False
    if v.startswith(KEY_PREFIXES):
        return True
    return len(v) > 60 and "://" not in v and "/" not in v and " " not in v

# ---------- 霓虹主题常量 ----------
FRAME_MARGIN = 11          # 外框与窗口边缘的留白，用来给外发光腾位置
FRAME_RADIUS = 0.0         # 0 = 纯矩形，保留 Windows XP 的方角窗口形态
BORDER_BAND = 34           # 只重绘这么宽的边框区域（含外发光 + 灯头光斑）
RING_SAMPLES = 720         # 边框周长采样点数（缓存后每帧只取下标）
NEON_PINK = QColor("#FF2E93")
NEON_GREEN = QColor("#12F5A0")
NEON_BLUE = QColor("#2C6BFF")
NEON_VIOLET = QColor("#B44BFF")
NEON_CYAN = QColor("#7CF9FF")

BAD_BASE_QSS = ("QLineEdit { border: 2px solid #FF3B5C;"
                " background: #FFE3E8; color: #4A0010;"
                " padding: 1px 7px; }")

QSS = """
QWidget#Root { background: transparent; }
QWidget {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI";
    font-size: 9pt;
    color: #F4F8FF;
}
QLabel { background: transparent; color: #F4F8FF; }
QLabel#Hint {
    color: rgba(226, 238, 255, 0.86);
    font-size: 8.4pt;
}
QLabel#Status {
    background: rgba(6, 10, 24, 0.66);
    border: 1px solid rgba(180, 240, 255, 0.40);
    border-top-color: rgba(255, 255, 255, 0.55);
    padding: 2px 10px;
    color: #DFF4FF;
}
QGroupBox {
    background: rgba(9, 13, 32, 0.48);
    border: 1px solid rgba(180, 235, 255, 0.45);
    border-top-color: rgba(255, 255, 255, 0.55);
    margin-top: 0px;
    padding: 26px 12px 9px 12px;
    font-weight: bold;
    color: #FFFFFF;
}
QGroupBox::title {
    subcontrol-origin: padding;
    subcontrol-position: top left;
    left: 11px;
    top: 7px;
    padding: 0px 1px;
    background: transparent;
    color: #CFF3FF;
}
QLineEdit, QSpinBox, QComboBox {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #FFFFFF, stop:1 #E7EDFA);
    border: 1px solid #7C87AD;
    border-top-color: #46527A;
    border-left-color: #46527A;
    padding: 2px 7px;
    min-height: 17px;
    color: #111730;
    selection-background-color: #FF2E93;
    selection-color: #FFFFFF;
}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover {
    background: #FFFFFF;
    border: 1px solid #8C97BD;
    border-top-color: #46527A;
    border-left-color: #46527A;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    background: #FFFFFF;
    border: 1px solid #2C6BFF;
    border-top-color: #7CF9FF;
    border-left-color: #7CF9FF;
}
QLineEdit:read-only {
    background: #EFF3FC;
    color: #3A4366;
    font-family: "Consolas", "Cascadia Mono", "Courier New";
}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {
    background: rgba(255, 255, 255, 0.30);
    color: rgba(255, 255, 255, 0.62);
    border: 1px solid rgba(255, 255, 255, 0.24);
}
QSpinBox::up-button, QSpinBox::down-button {
    width: 15px;
    border: none;
    background: #EDF1FB;
    margin-right: 1px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #DCE6FA; }
QSpinBox::up-arrow { image: url("@UP@"); width: 11px; height: 9px; }
QSpinBox::down-arrow { image: url("@DOWN@"); width: 11px; height: 9px; }
QComboBox::drop-down {
    border: none;
    border-left: 1px solid #C6D0E8;
    width: 19px;
    background: #EDF1FB;
}
QComboBox::drop-down:hover { background: #DCE6FA; }
QComboBox::down-arrow { image: url("@DOWN@"); width: 11px; height: 9px; }
QComboBox QAbstractItemView {
    background-color: #FFFFFF;
    color: #111730;
    border: 2px solid #7CF9FF;
    padding: 2px;
    outline: none;
    selection-background-color: #FF2E93;
    selection-color: #FFFFFF;
}
QCheckBox {
    background: transparent;
    color: #F4F8FF;
    spacing: 6px;
}
QCheckBox:disabled { color: rgba(255, 255, 255, 0.42); }
QCheckBox::indicator {
    width: 13px;
    height: 13px;
    border: 1px solid #CBD5EB;
    border-top-color: #5E6A8E;
    border-left-color: #5E6A8E;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #FFFFFF, stop:1 #E2E9F6);
}
QCheckBox::indicator:hover { border: 1px solid #FF2E93; }
QCheckBox::indicator:checked {
    border: 1px solid #FFFFFF;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #FF2E93, stop:0.55 #B44BFF, stop:1 #2C6BFF);
    image: url("@CHECK@");
}
QCheckBox::indicator:disabled {
    background: rgba(255, 255, 255, 0.16);
    border: 1px solid rgba(255, 255, 255, 0.24);
}
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #FFFFFF, stop:0.45 #F1F5FF, stop:1 #D2DCF3);
    border: 1px solid #5F6C96;
    border-top-color: #FFFFFF;
    border-left-color: #F6F9FF;
    padding: 4px 16px;
    min-width: 74px;
    min-height: 21px;
    color: #16203F;
    font-weight: bold;
}
QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #FFFFFF, stop:0.42 #FFEAF6, stop:1 #F2C6E4);
    border: 1px solid #FF2E93;
    color: #3A0022;
}
QPushButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #C6D2EC, stop:1 #E6ECF9);
    border: 1px solid #8C97B8;
    border-top-color: #59637E;
    border-left-color: #59637E;
    padding: 5px 15px 3px 17px;
}
QPushButton:disabled {
    color: #8A93AE;
    background: #DBE2F0;
    border: 1px solid #B7BFD4;
    border-top-color: #F2F5FB;
}
QPushButton#Primary {
    min-width: 100px;
    font-size: 9.6pt;
    color: #FFFFFF;
    border: 1px solid #FFFFFF;
    border-bottom-color: #6B0E52;
    border-right-color: #1B3F9E;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #FF2E93, stop:0.5 #B44BFF, stop:1 #2C6BFF);
}
QPushButton#Primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #FF5CAB, stop:0.5 #C86BFF, stop:1 #5A8CFF);
}
QPushButton#Primary:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #C61868, stop:0.5 #8A35CC, stop:1 #1745C4);
    padding: 5px 15px 3px 17px;
}
QPushButton#Primary:disabled {
    color: rgba(255, 255, 255, 0.55);
    background: #7C7FA8;
    border: 1px solid #A9ACCB;
}
QPushButton#MinBtn, QPushButton#CloseBtn {
    min-width: 21px; max-width: 21px;
    min-height: 19px; max-height: 19px;
    padding: 0px;
    color: #FFFFFF;
    font-weight: bold;
    font-size: 9pt;
}
QPushButton#MinBtn {
    border: 1px solid #FFFFFF;
    border-bottom-color: #24478F;
    border-right-color: #24478F;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #86B8FF, stop:1 #2A5FCF);
}
QPushButton#MinBtn:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #B4D4FF, stop:1 #4A7FE0);
}
QPushButton#CloseBtn {
    border: 1px solid #FFFFFF;
    border-bottom-color: #7A1024;
    border-right-color: #7A1024;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #FF8FA0, stop:1 #CC1F3B);
}
QPushButton#CloseBtn:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #FFB0BD, stop:1 #E2364F);
}
QMenuBar {
    background: rgba(8, 12, 30, 0.44);
    border: none;
    padding: 0px 4px;
}
QMenuBar::item {
    padding: 4px 10px;
    background: transparent;
    color: rgba(255, 255, 255, 0.92);
}
QMenuBar::item:selected {
    background: rgba(255, 255, 255, 0.24);
    color: #FFFFFF;
}
QMenu {
    background-color: #141A33;
    border: 2px solid #7CF9FF;
    padding: 3px;
    color: #EAF2FF;
}
QMenu::item { padding: 5px 26px 5px 18px; color: #EAF2FF; }
QMenu::item:selected { background-color: #FF2E93; color: #FFFFFF; }
QMenu::separator { height: 1px; background: rgba(255, 255, 255, 0.16); margin: 4px 8px; }
QDialog { background-color: #101529; }
QPlainTextEdit {
    background-color: rgba(6, 10, 24, 0.88);
    border: 1px solid rgba(180, 235, 255, 0.40);
    padding: 6px;
    color: #DDEBFF;
    font-family: "Consolas", "Cascadia Mono", "Courier New";
    selection-background-color: #FF2E93;
}
QScrollBar:vertical {
    background: rgba(255, 255, 255, 0.06);
    width: 11px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #FF2E93, stop:0.6 #B44BFF, stop:1 #2C6BFF);
    min-height: 22px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
"""


def dark_text_palette(widget):
    """把控件的前景色拉回深色：QSS 让全局文字变白后，Fusion 画的
    QSpinBox 上下箭头 / QComboBox 下拉箭头会跟着变白，在浅色输入框里看不见。"""
    pal = widget.palette()
    for role in (QPalette.ButtonText, QPalette.Text, QPalette.WindowText):
        pal.setColor(role, QColor("#16203F"))
    widget.setPalette(pal)
    return widget


def _arrow_png(direction):
    """现画一枚深色箭头小图。Fusion 自带的箭头在 QSS 下会被染成白色，
    所以这里自己出图，QSS 用 image: url() 引用。"""
    pm = QPixmap(11, 9)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    if direction == "up":
        path.moveTo(0.8, 7.2)
        path.lineTo(10.2, 7.2)
        path.lineTo(5.5, 1.6)
    else:
        path.moveTo(0.8, 1.6)
        path.lineTo(10.2, 1.6)
        path.lineTo(5.5, 7.2)
    path.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#22305A"))
    p.drawPath(path)
    p.end()
    return pm


def _check_png():
    """勾选态的对勾（白色），叠在霓虹渐变方块上。"""
    pm = QPixmap(11, 11)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor("#FFFFFF"), 1.9)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawPolyline(QPointF(2.0, 5.6), QPointF(4.4, 8.0), QPointF(9.0, 2.8))
    p.end()
    return pm


def styled_qss():
    """把 QSS 里的箭头 / 对勾占位符替换成运行时生成的小图路径。"""
    folder = os.path.join(tempfile.gettempdir(), "poker-neon-ui")
    assets = {"up": _arrow_png("up"), "down": _arrow_png("down"),
              "check": _check_png()}
    try:
        os.makedirs(folder, exist_ok=True)
        for name, pm in assets.items():
            pm.save(os.path.join(folder, f"{name}.png"))
    except OSError:
        return (QSS.replace('image: url("@UP@");', "")
                   .replace('image: url("@DOWN@");', "")
                   .replace('image: url("@CHECK@");', ""))
    sub = {"@UP@": "up", "@DOWN@": "down", "@CHECK@": "check"}
    out = QSS
    for token, name in sub.items():
        out = out.replace(token, os.path.join(folder, f"{name}.png").replace("\\", "/"))
    return out


def paint_soft_shadow(painter, rect, blur, dx, dy, color):
    """画一块柔和投影。QGraphicsDropShadowEffect 在无边框+半透明窗口下
    每帧都要重渲染整棵子树，占 CPU 太高；这里缩小-放大一次做简易模糊，
    只在背景缓存（resize 时）跑一遍。"""
    pad = max(3, int(blur * 0.85))
    body = QRectF(rect).translated(dx, dy)
    f = max(2.0, blur / 2.0)
    sw = max(1, int((body.width() + pad * 2) / f))
    sh = max(1, int((body.height() + pad * 2) / f))
    small = QPixmap(sw, sh)
    small.fill(Qt.transparent)
    sp = QPainter(small)
    sp.setRenderHint(QPainter.Antialiasing, True)
    sp.setPen(Qt.NoPen)
    sp.setBrush(color)
    sp.drawRect(QRectF(pad / f, pad / f, body.width() / f, body.height() / f))
    sp.end()
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.drawPixmap(
        QRectF(body.left() - pad, body.top() - pad,
               body.width() + pad * 2, body.height() + pad * 2),
        small, QRectF(small.rect()))


def read_saved_config() -> dict:
    """读取同目录 poker-config.bat 里的 JEV_* 值（不打印 key）。"""
    out: dict[str, str] = {}
    if not os.path.isfile(CONFIG_BAT):
        return out
    try:
        with open(CONFIG_BAT, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = re.match(r'\s*set\s+"?([A-Za-z_]+)=([^"]*)"?\s*$', line)
                if m and m.group(1).upper().startswith("JEV_"):
                    out[m.group(1).upper()] = m.group(2).strip().strip('"')
    except OSError:
        pass
    return out


class TitleBar(QWidget):
    """自绘霓虹玻璃标题栏（无边框窗口，自己处理拖动）。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedHeight(34)
        self._drag = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 8, 0)
        lay.setSpacing(8)

        icon = QLabel("♠", self)
        icon.setStyleSheet("color:#FFFFFF; font-size:15pt; font-weight:bold;")
        lay.addWidget(icon)
        title = QLabel("德州扑克  ·  启动设置", self)
        title.setStyleSheet("color:#FFFFFF; font-weight:bold; font-size:9.5pt;"
                            "letter-spacing:1px;")
        lay.addWidget(title)
        lay.addStretch(1)

        btn_min = QPushButton("−", self)
        btn_min.setObjectName("MinBtn")
        btn_min.setToolTip("最小化")
        btn_min.clicked.connect(parent.showMinimized)
        lay.addWidget(btn_min)

        btn_close = QPushButton("×", self)
        btn_close.setObjectName("CloseBtn")
        btn_close.setToolTip("关闭")
        btn_close.clicked.connect(QApplication.instance().quit)
        lay.addWidget(btn_close)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect())
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0.0, QColor(7, 10, 26, 132))
        g.setColorAt(1.0, QColor(7, 10, 26, 0))
        p.fillRect(r, g)
        # 底部一条会呼吸的霓虹细线，把标题栏和内容区分开
        lg = QLinearGradient(0, 0, self.width(), 0)
        lg.setColorAt(0.0, QColor(124, 249, 255, 0))
        lg.setColorAt(0.25, QColor(255, 255, 255, 120))
        lg.setColorAt(0.6, QColor(124, 249, 255, 130))
        lg.setColorAt(1.0, QColor(124, 249, 255, 0))
        p.fillRect(QRectF(10, self.height() - 1.4, self.width() - 20, 1.4), lg)
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPos() - self.window().frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPos() - self._drag)
            event.accept()

    def mouseReleaseEvent(self, _event):
        self._drag = None


class ProbeWorker(QThread):
    """自检 / 模型列表在后台跑，避免卡住 UI。"""
    done = pyqtSignal(str)

    def __init__(self, cmd, env):
        super().__init__()
        self.cmd, self.env = cmd, env

    def run(self):
        try:
            p = subprocess.run(self.cmd, env=self.env, cwd=HERE,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=90)
            out = ((p.stdout or "") + (p.stderr or "")).strip() or "(没有输出)"
        except Exception as e:                                  # noqa: BLE001
            out = f"执行失败：{e}"
        self.done.emit(out)


class OutputDialog(QDialog):
    def __init__(self, parent, title, text):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(620, 380)
        lay = QVBoxLayout(self)
        head = QLabel(title, self)
        head.setStyleSheet("color:#7CF9FF; font-weight:bold; font-size:9.5pt;")
        lay.addWidget(head)
        box = QPlainTextEdit(self)
        box.setReadOnly(True)
        box.setPlainText(text)
        lay.addWidget(box, 1)
        btn = QPushButton("关闭", self)
        btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn)
        lay.addLayout(row)


class Launcher(QWidget):
    def __init__(self):
        super().__init__()
        self.saved = read_saved_config()
        self.busy = False
        self.workers = []
        self.prev_n = 5
        self._bg_cache = None
        self._phase = 0.0
        self._shadows = []
        self._ring_pts = None

        self.setWindowTitle("德州扑克 · 启动设置")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowMinimizeButtonHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("Root")
        self.setFixedSize(566, 846)
        self.center()

        m = FRAME_MARGIN
        root = QVBoxLayout(self)
        root.setContentsMargins(m + 8, m + 4, m + 8, m + 5)
        root.setSpacing(6)
        root.addWidget(TitleBar(self))
        root.addWidget(self._build_menubar())
        root.addWidget(self._build_table_group())
        root.addWidget(self._build_model_group())
        root.addLayout(self._build_preview())
        root.addLayout(self._build_buttons())
        self.status = QLabel("就绪 · 设置好后点「启动游戏」", self)
        self.status.setObjectName("Status")
        self.status.setFixedHeight(24)
        root.addWidget(self.status)
        for cls in (QSpinBox, QComboBox, QLineEdit):
            for w in self.findChildren(cls):
                dark_text_palette(w)
        self.refresh()

        # 霓虹流水灯：25fps 只重绘四周光带，静态灯管走缓存
        self._neon = QTimer(self)
        self._neon.timeout.connect(self._tick)
        self._neon.start(40)

    # ---------- 霓虹外观 ----------
    def _frame_rect(self):
        m = FRAME_MARGIN
        return QRectF(m, m, self.width() - 2 * m, self.height() - 2 * m)

    def _add_shadow(self, widget, color, blur=22, dy=5, dx=0):
        """登记一块要画的投影，实际绘制在背景缓存里（比 QGraphicsEffect 省太多 CPU）。"""
        self._shadows.append((widget, color, blur, dx, dy))

    def _border_path(self):
        fr = self._frame_rect()
        path = QPainterPath()
        path.addRect(fr.adjusted(1.6, 1.6, -1.6, -1.6))
        return path

    def _border_region(self):
        """只重绘四周的发光带：整窗 30fps 重绘会把 CPU 吃到 40%。"""
        outer = QRect(0, 0, self.width(), self.height())
        inner = QRect(outer).adjusted(BORDER_BAND, BORDER_BAND,
                                      -BORDER_BAND, -BORDER_BAND)
        return QRegion(outer) - QRegion(inner)

    def _tick(self):
        if not self.isVisible():
            return
        self._phase = (self._phase + 0.0066) % 1.0
        self.update(self._border_region())

    def resizeEvent(self, event):
        self._bg_cache = None
        self._ring_pts = None
        super().resizeEvent(event)

    def changeEvent(self, event):
        # 窗口没焦点时降频到 ~11fps：切出去打游戏时别白占 CPU
        if event.type() == QEvent.WindowActivate:
            self._neon.start(40)
        elif event.type() == QEvent.WindowDeactivate:
            self._neon.start(90)
        super().changeEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._bg_cache is None or self._bg_cache.size() != self.size():
            self._bg_cache = self._render_background()
        p.drawPixmap(0, 0, self._bg_cache)
        self._paint_comet(p)
        p.end()

    def _render_background(self):
        """高饱和「粉 → 绿 → 蓝」渐变底：直接 RGB 插值会发灰，
        所以主渐变走 粉→紫→蓝，再用叠加混合补一团绿光。
        静止的霓虹灯管也画进这张缓存图，动画帧只重画流光。"""
        dpr = self.devicePixelRatioF()
        w, h = self.width(), self.height()
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        fr = self._frame_rect()
        body = QPainterPath()
        body.addRect(fr)
        p.save()
        p.setClipPath(body)
        g = QLinearGradient(fr.left(), fr.top(), fr.right(), fr.bottom())
        g.setColorAt(0.00, QColor("#FF2E93"))
        g.setColorAt(0.24, QColor("#FF4FB0"))
        g.setColorAt(0.50, QColor("#9B4DFF"))
        g.setColorAt(0.76, QColor("#2C6BFF"))
        g.setColorAt(1.00, QColor("#0FB6FF"))
        p.fillRect(fr, g)

        # 真·绿：用 SourceOver 铺一块绿光，Plus 混合会直接烧成青色/白色
        green = QRadialGradient(fr.left() + fr.width() * 0.26,
                                fr.bottom() - fr.height() * 0.02,
                                fr.width() * 0.95)
        green.setColorAt(0.0, QColor(0, 255, 138, 255))
        green.setColorAt(0.42, QColor(10, 246, 148, 190))
        green.setColorAt(0.78, QColor(18, 245, 160, 55))
        green.setColorAt(1.0, QColor(18, 245, 160, 0))
        p.fillRect(fr, green)

        p.setCompositionMode(QPainter.CompositionMode_Plus)
        pink = QRadialGradient(fr.left() + fr.width() * 0.88,
                               fr.top() + fr.height() * 0.05,
                               fr.width() * 0.78)
        pink.setColorAt(0.0, QColor(255, 46, 147, 140))
        pink.setColorAt(0.6, QColor(255, 46, 147, 36))
        pink.setColorAt(1.0, QColor(255, 46, 147, 0))
        p.fillRect(fr, pink)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)

        # 底部压暗：底部是亮青/亮绿，白字按钮和状态栏需要一块暗底才读得清
        scrim = QLinearGradient(fr.left(), fr.top() + fr.height() * 0.58,
                                fr.left(), fr.bottom())
        scrim.setColorAt(0.0, QColor(6, 10, 26, 0))
        scrim.setColorAt(1.0, QColor(6, 10, 26, 118))
        p.fillRect(fr, scrim)

        sheen = QLinearGradient(fr.left(), fr.top(),
                                fr.left(), fr.top() + fr.height() * 0.40)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 44))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(fr, sheen)
        p.restore()

        # 卡片 / 按钮的柔和投影（画在背景层，控件本身画在上面）
        for widget, color, blur, dx, dy in self._shadows:
            if widget.isVisible():
                paint_soft_shadow(p, QRectF(widget.geometry()), blur, dx, dy, color)

        self._draw_tube(p)
        p.end()
        return pm

    def _draw_tube(self, p):
        """静止的霓虹灯管：锥形渐变 + 多层外发光 + 内侧压暗边。"""
        fr = self._frame_rect()
        path = self._border_path()
        cg = QConicalGradient(fr.center().x(), fr.center().y(), -60.0)
        cg.setColorAt(0.00, NEON_PINK)
        cg.setColorAt(0.30, NEON_GREEN)
        cg.setColorAt(0.60, NEON_BLUE)
        cg.setColorAt(0.84, NEON_VIOLET)
        cg.setColorAt(1.00, NEON_PINK)

        p.save()
        p.setBrush(Qt.NoBrush)
        # 先在内侧压一条暗边：底色是高饱和渐变，不压暗霓虹管根本显不出来
        dark = QPainterPath()
        dark.addRect(fr.adjusted(2.2, 2.2, -2.2, -2.2))
        pen = QPen(QColor(4, 8, 20, 105), 1.4)
        pen.setJoinStyle(Qt.MiterJoin)
        p.setPen(pen)
        p.drawPath(dark)
        for width, alpha in ((17.0, 14), (11.0, 24), (6.5, 40), (3.4, 66)):
            p.setOpacity(alpha / 255.0)
            pen = QPen(QBrush(cg), width)
            pen.setJoinStyle(Qt.MiterJoin)
            p.setPen(pen)
            p.drawPath(path)
        p.setOpacity(1.0)
        pen = QPen(QColor(255, 255, 255, 62), 1.0)
        pen.setJoinStyle(Qt.MiterJoin)
        p.setPen(pen)
        p.drawPath(path)
        p.restore()

    def _ring_points(self):
        """把边框周长采样成一张点表：QPainterPath.pointAtPercent 每帧调 130 次
        太贵（内部要按长度遍历路径），缓存下来后每帧只做下标取点。"""
        pts = self._ring_pts
        if pts is None:
            path = self._border_path()
            pts = [path.pointAtPercent(i / RING_SAMPLES)
                   for i in range(RING_SAMPLES)]
            self._ring_pts = pts
        return pts

    def _paint_comet(self, p):
        """流动的高亮光带：沿周长取点，点叠点糊成一条连续光带。"""
        pts = self._ring_points()
        n = len(pts)
        head = self._phase
        p.save()
        p.setBrush(Qt.NoBrush)
        steps = 110
        tail = 0.13
        for i in range(steps):
            f = 1.0 - i / (steps - 1.0)
            t = (head - tail * (i / (steps - 1.0))) % 1.0
            pt = pts[int(t * n) % n]
            if pt.isNull():
                continue
            alpha = int(250 * (f ** 1.5))
            if alpha <= 4:
                continue
            p.setPen(QPen(QColor(226, 253, 255, alpha),
                          1.2 + 5.4 * (f ** 1.3), Qt.SolidLine, Qt.RoundCap))
            p.drawPoint(pt)

        # 头部白热芯：让「灯头」在亮底上仍然看得清
        for i in range(14):
            f = 1.0 - i / 13.0
            t = (head - 0.055 * (i / 13.0)) % 1.0
            pt = pts[int(t * n) % n]
            if pt.isNull():
                continue
            p.setPen(QPen(QColor(255, 255, 255, int(235 * (f ** 1.6))),
                          0.8 + 3.2 * f, Qt.SolidLine, Qt.RoundCap))
            p.drawPoint(pt)

        hp = pts[int(head * n) % n]
        if not hp.isNull():
            glow = QRadialGradient(hp, 18.0)
            glow.setColorAt(0.0, QColor(255, 255, 255, 245))
            glow.setColorAt(0.30, QColor(190, 240, 255, 150))
            glow.setColorAt(1.0, QColor(124, 249, 255, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawEllipse(hp, 18.0, 18.0)
        p.restore()

    def center(self):
        from PyQt5.QtWidgets import QDesktopWidget
        geo = QDesktopWidget().availableGeometry()
        self.move((geo.width() - self.width()) // 2,
                  max(0, (geo.height() - self.height()) // 2 - 20))

    # ---------- 菜单 ----------
    def _build_menubar(self):
        mb = QMenuBar(self)
        mb.setNativeMenuBar(False)   # 强制 Qt 自绘，避免某些 Windows 主题下菜单文字不显示
        # 高度必须够放下一个菜单项，否则 Qt 会把「文件/帮助」全部折叠成右侧的 »。
        mb.setFixedHeight(max(34, mb.sizeHint().height()))
        f = mb.addMenu("文件(&F)")
        f.addAction("启动游戏", self.launch_game)
        f.addAction("连通自检", lambda: self.launch_probe("--check", "连通自检"))
        f.addSeparator()
        f.addAction("退出(&X)", QApplication.instance().quit)
        h = mb.addMenu("帮助(&H)")
        h.addAction("使用说明", self.show_help)
        h.addAction("关于", lambda: self._about())
        return mb

    def show_help(self):
        OutputDialog(self, "使用说明", (
            "机器人数：1 = 单挑，5 = 6 人桌（你 + 5 个机器人）。\n\n"
            "模式：\n"
            "  · 现金局：谁的筹码见底，就问你「补筹码继续 / 退出」。\n"
            "  · 淘汰赛：筹码见底的座位直接出局（会报出「第 N 名」），"
            "一直打到只剩最后一个人，最后那位就是冠军。\n"
            "涨盲：每 N 局盲注翻倍，0 = 不涨。淘汰赛建议 8~12 局，"
            "否则短码互相磨会拖很久。\n\n"
            "记忆局数：机器人记住最近多少局的对手画像（松/紧、爱不爱弃牌），"
            "Jev 和本地 AI 共用这份记忆；0 = 关闭，每局都失忆。默认 10 局，"
            "记录存同目录 poker-memory.json，想清零用 --forget 或删掉该文件。\n\n"
            "在线座位：先勾选「启用在线模型」，再逐个勾选哪些机器走模型。"
            "每一行还能单独设「风格」和「模型」——\n"
            "  · 风格：随机 / 稳健 / 激进 / 紧凶 / 松凶 / 跟注站。默认 = 按座位轮换，"
            "「随机」= 开局从风格池里抽（一桌尽量不重样）；本地 AI 也认这个风格"
            "（会影响它的松紧和诈唬频率）。局内看不到谁是什么风格，打完退出时才揭晓。\n"
            "  · 模型：留「跟随全局」就用上面的全局模型；填别的名字（例如 jev-latest、"
            "gpt-4o-mini）则这个座位单独用那个模型。不知道有哪些名字时就点「模型列表」。\n"
            "未勾选的座位走本地兜底 AI，不联网、不花钱。\n\n"
            "服务商：只填 TypeSafe 官方 apikey_ 开头的 key 时选「自动识别」即可；"
            "第三方聊天网关要填 API 地址，可点「模型列表」拉取真实 model id。\n\n"
            "API 地址只填网址（https://…/v1，不要带 /chat/completions）。"
            "如果把 apikey_ 开头的 Key 粘进了「API 地址」，启动器会自动把它挪回 Key 字段、"
            "清空地址并改用官方 Jev 端点；输入框会变红提醒。\n\n"
            "API Key 留空 = 沿用同目录 poker-config.bat / 环境变量里已保存的值；"
            "输入框里的 Key 只通过环境变量传给游戏进程，不会写进日志。\n\n"
            "游戏内热键：f 弃牌 / c 跟注或过牌 / r<额> 加注 / a 全下 / x 老板键 / ? 帮助。")
        ).exec_()

    def _about(self):
        OutputDialog(self, "关于", "德州扑克 · 霓虹启动器\n\n"
                                   "界面：PyQt5 自绘渐变 + 霓虹流光边框\n"
                                   "游戏本体：纯 Python 标准库，零依赖\n"
                                   "在线模型：TypeSafe Jev / OpenAI 兼容网关").exec_()

    # ---------- 布局小工具 ----------
    def _row(self, parent, label, width=82):
        """固定标签宽度的横向行，避免 QGridLayout 各列宽互相牵连导致控件重叠。"""
        row = QHBoxLayout()
        row.setSpacing(6)
        row.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label, parent)
        lab.setFixedWidth(width)
        row.addWidget(lab)
        return row

    # ---------- 牌桌设置 ----------
    def _build_table_group(self):
        g = QGroupBox("牌桌设置", self)
        self._add_shadow(g, QColor(3, 6, 22, 175), 26, 7)
        v = QVBoxLayout(g)
        v.setSpacing(7)
        v.setContentsMargins(2, 2, 2, 2)

        self.bots = QSpinBox(g)
        self.bots.setRange(1, 5)
        self.bots.setValue(5)
        self.bots.setFixedWidth(62)
        r1 = self._row(g, "机器人数")
        r1.addWidget(self.bots)
        r1.addSpacing(8)
        r1.addWidget(QLabel("记忆局数", g))
        self.memory = QSpinBox(g)
        self.memory.setRange(0, 50)
        self.memory.setValue(MEMORY_WINDOW_DEFAULT)
        self.memory.setFixedWidth(62)
        self.memory.setToolTip("机器人记住最近多少局的对手画像（松紧/爱不爱弃牌）；0 = 关闭记忆")
        r1.addWidget(self.memory)
        note = QLabel("1=单挑 5=六人桌", g)
        note.setObjectName("Hint")
        r1.addWidget(note)
        r1.addStretch(1)
        v.addLayout(r1)

        self.sb = QLineEdit("10", g)
        self.bb = QLineEdit("20", g)
        self.stack = QLineEdit("1000", g)
        for e in (self.sb, self.bb, self.stack):
            e.setFixedWidth(66)
        r2 = self._row(g, "盲注")
        r2.addWidget(self.sb)
        r2.addWidget(QLabel("/", g))
        r2.addWidget(self.bb)
        r2.addWidget(QLabel("   起始筹码", g))
        r2.addWidget(self.stack)
        r2.addStretch(1)
        v.addLayout(r2)

        self.cards = QComboBox(g)
        for text, _code in CARD_STYLES:
            self.cards.addItem(text)
        self.cards.setFixedWidth(196)
        self.reasons = QCheckBox("局末显示下注理由", g)
        self.reasons.setChecked(True)
        r3 = self._row(g, "牌面")
        r3.addWidget(self.cards)
        r3.addSpacing(10)
        r3.addWidget(self.reasons)
        r3.addStretch(1)
        v.addLayout(r3)

        self.mode = QComboBox(g)
        for text, _code in MODES:
            self.mode.addItem(text)
        self.mode.setFixedWidth(206)
        self.mode.setToolTip("现金局：筹码见底可以补筹码接着打\n"
                             "淘汰赛：筹码见底的座位直接出局，一直打到只剩最后一个人")
        self.blind_up = QSpinBox(g)
        self.blind_up.setRange(0, 50)
        self.blind_up.setValue(10)
        self.blind_up.setFixedWidth(62)
        self.blind_up.setToolTip("每 N 局盲注翻倍，0 = 不涨。淘汰赛建议 8~12 局，"
                                 "否则短码相持会拖很久")
        self.blind_hint = QLabel("局一涨盲", g)
        r4 = self._row(g, "模式")
        r4.addWidget(self.mode)
        r4.addSpacing(8)
        r4.addWidget(self.blind_up)
        r4.addWidget(self.blind_hint)
        r4.addStretch(1)
        v.addLayout(r4)

        hint = QLabel("游戏里按 ? 看当前这手牌的白话讲解 · x 一键清屏", g)
        hint.setObjectName("Hint")
        v.addWidget(hint)

        self.bots.valueChanged.connect(self.refresh)
        self.memory.valueChanged.connect(self.refresh)
        for e in (self.sb, self.bb, self.stack):
            e.textChanged.connect(self.refresh)
        self.reasons.stateChanged.connect(self.refresh)
        self.cards.currentIndexChanged.connect(self.refresh)
        self.mode.currentIndexChanged.connect(self.refresh)
        self.blind_up.valueChanged.connect(self.refresh)
        return g

    # ---------- 模型设置 ----------
    def _build_model_group(self):
        g = QGroupBox("模型设置", self)
        self._add_shadow(g, QColor(3, 6, 22, 175), 26, 7)
        v = QVBoxLayout(g)
        v.setSpacing(7)
        v.setContentsMargins(2, 2, 2, 2)

        self.enable = QCheckBox(
            "启用在线模型（不勾 = 全部本地 AI，不联网不花钱）", g)
        self.enable.setChecked(False)          # 默认纯本地：开箱即玩，不联网不花钱
        v.addWidget(self.enable)

        self.provider = QComboBox(g)
        for text, _code in PROVIDERS:
            self.provider.addItem(text)
        self.provider.setFixedWidth(300)
        r1 = self._row(g, "服务商")
        r1.addWidget(self.provider)
        r1.addStretch(1)
        v.addLayout(r1)

        # 每个座位一行：是否在线 + 自己的风格 + 自己的模型（留空跟随全局）
        head = QHBoxLayout()
        head.setSpacing(6)
        for text, w in (("在线座位", 80), ("风格", 96), ("模型（留空=跟随全局）", 210)):
            lab = QLabel(text, g)
            lab.setObjectName("Hint")
            lab.setFixedWidth(w)
            head.addWidget(lab)
        head.addStretch(1)
        v.addLayout(head)

        self.seat_boxes = []
        self.seat_styles = []
        self.seat_models = []
        for i in range(5):
            row = QHBoxLayout()
            row.setSpacing(6)
            cb = QCheckBox(f"机器{i + 1}", g)
            cb.setFixedWidth(80)
            cb.setChecked(i == 0)
            cb.setToolTip("勾上 = 这个座位走在线模型（不勾 = 本地 AI，不产生调用费用）")
            style = QComboBox(g)
            style.addItems(STYLE_LABELS)
            style.setCurrentIndex(STYLE_TAGS.index("随机"))     # 默认随机
            style.setFixedWidth(96)
            style.setToolTip("这个座位自己的打法。默认「随机」= 开局从风格池里抽一个，\n"
                             "一桌尽量不重样，重开一场会重新抽；也可以写死成某一种。\n"
                             "本地 AI 也认这个风格（会影响松紧和诈唬频率）")
            model = QComboBox(g)
            model.setEditable(True)
            model.addItem(SEAT_FOLLOW)
            model.addItem("jev-latest")
            model.addItem("deepseek-flash")
            model.setFixedWidth(210)
            model.setToolTip("这个座位单独用哪个模型；留「跟随全局」就用上面的全局模型。\n"
                             "不知道有哪些模型名时点「模型列表」看")
            row.addWidget(cb)
            row.addWidget(style)
            row.addWidget(model)
            row.addStretch(1)
            v.addLayout(row)
            self.seat_boxes.append(cb)
            self.seat_styles.append(style)
            self.seat_models.append(model)
            cb.stateChanged.connect(self.refresh)
            style.currentIndexChanged.connect(self.refresh)
            model.currentIndexChanged.connect(self.refresh)
            model.editTextChanged.connect(self.refresh)

        self.base = QLineEdit(self.saved.get("JEV_BASE_URL", ""), g)
        self.base.setPlaceholderText("留空 = 自动识别官方 Jev 端点 · Key 别填这里")
        r3 = self._row(g, "API 地址")
        r3.addWidget(self.base, 1)
        v.addLayout(r3)

        self.model = QLineEdit(self.saved.get("JEV_MODEL", ""), g)
        self.model.setPlaceholderText("留空 = 自动")
        self.key = QLineEdit("", g)
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("留空 = 沿用已有")
        r4 = self._row(g, "模型名")
        r4.addWidget(self.model, 1)
        r4.addWidget(QLabel("API Key", g))
        r4.addWidget(self.key, 1)
        v.addLayout(r4)

        hint_text = ("已读取 poker-config.bat 中保存的配置（Key 不显示）"
                     if self.saved.get("JEV_API_KEY") else
                     "留空 = 沿用 poker-config.bat / 环境变量；Key 不会明文显示")
        self.cfg_hint = QLabel(hint_text, g)
        self.cfg_hint.setObjectName("Hint")
        v.addWidget(self.cfg_hint)
        hint2 = QLabel("只有勾选的座位会调用模型 · 提前弃牌的局几乎不产生费用", g)
        hint2.setObjectName("Hint")
        v.addWidget(hint2)

        self.model_controls = [self.provider, self.base, self.model, self.key]
        self.enable.stateChanged.connect(self.refresh)
        for e in (self.base, self.model, self.key):
            e.textChanged.connect(self.refresh)
        self.provider.currentIndexChanged.connect(self.refresh)
        return g

    # ---------- 预览与按钮 ----------
    def _build_preview(self):
        row = QVBoxLayout()
        row.setSpacing(3)
        lab = QLabel("命令行参数预览", self)
        lab.setObjectName("Hint")
        row.addWidget(lab)
        self.preview = QLineEdit(self)
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(23)
        row.addWidget(self.preview)
        return row

    def _build_buttons(self):
        row = QHBoxLayout()
        row.setSpacing(9)
        b1 = QPushButton("启动游戏", self)
        b1.setObjectName("Primary")
        b1.setDefault(True)
        b1.clicked.connect(self.launch_game)
        self._add_shadow(b1, QColor(255, 46, 147, 165), 24, 4)
        b2 = QPushButton("连通自检", self)
        b2.clicked.connect(lambda: self.launch_probe("--check", "连通自检"))
        self._add_shadow(b2, QColor(4, 8, 24, 170), 18, 4)
        b3 = QPushButton("模型列表", self)
        b3.clicked.connect(lambda: self.launch_probe("--models", "模型列表"))
        self._add_shadow(b3, QColor(4, 8, 24, 170), 18, 4)
        b4 = QPushButton("退出", self)
        b4.clicked.connect(QApplication.instance().quit)
        self._add_shadow(b4, QColor(4, 8, 24, 150), 16, 3)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addWidget(b3)
        row.addStretch(1)
        row.addWidget(b4)
        return row

    # ---------- 同步与命令拼装 ----------
    def refresh(self, *_args):
        if self.busy:
            return
        self.busy = True
        try:
            on = self.enable.isChecked()
            n = self.bots.value()
            shrank = n < self.prev_n
            for i, cb in enumerate(self.seat_boxes):
                live = i < n
                cb.setEnabled(live and on)
                # 风格对本地 AI 同样生效，所以只有超出人数时才禁用
                self.seat_styles[i].setEnabled(live)
                self.seat_models[i].setEnabled(live and on and cb.isChecked())
                if not live and cb.isChecked():
                    cb.blockSignals(True)
                    cb.setChecked(False)
                    cb.blockSignals(False)
            # 减少人数时不小心把在线座位全清掉了：保底留一个，别静默变纯离线。
            if shrank and on and not any(
                    self.seat_boxes[i].isChecked() for i in range(n)):
                cb = self.seat_boxes[0]
                cb.blockSignals(True)
                cb.setChecked(True)
                cb.blockSignals(False)
            self.prev_n = n
            # 涨盲只对淘汰赛有意义（现金局补筹码，不需要逼进度）
            tourney = MODES[self.mode.currentIndex()][1] == "tournament"
            self.blind_up.setEnabled(tourney)
            self.blind_hint.setEnabled(tourney)
            for w in self.model_controls + [self.cfg_hint]:
                w.setEnabled(on)
            raw_base = self.base.text().strip()
            bad_base = bool(raw_base) and not raw_base.startswith(("http://", "https://"))
            self.base.setStyleSheet(BAD_BASE_QSS if bad_base else "")
            args, _env, warn = self.collect()
            self.preview.setText("play.ps1 " + " ".join(args))
            # 5 人淘汰赛的命令比窗口宽，尾巴会被切掉：悬停看完整版
            self.preview.setToolTip("play.ps1 " + " ".join(args))
            self.preview.setCursorPosition(0)      # 参数变长后别只看到尾巴
            self.status.setText(warn or "就绪 · 设置好后点「启动游戏」")
        finally:
            self.busy = False

    def _relocate_pasted_key(self, key):
        """把误填进「API 地址」的 Key 搬进 Key 输入框，让用户看得见已经改对。

        只改界面表现，不动已保存配置；blockSignals 防止 setText 反过来再触发 refresh。
        """
        if self.base.text().strip():
            self.base.blockSignals(True)
            self.base.setText("")
            self.base.blockSignals(False)
            self.base.setStyleSheet("")
        if key and not self.key.text().strip():
            self.key.blockSignals(True)
            self.key.setText(key)
            self.key.blockSignals(False)
        # 聊天模型的 model id 对 Jev 无意义，留着会把 Jev key 发给聊天网关
        if self.model.text().strip() in DEEPSEEK_MODELS:
            self.model.blockSignals(True)
            self.model.setText("")
            self.model.blockSignals(False)

    def collect(self):
        n = self.bots.value()
        seats = [i + 1 for i, cb in enumerate(self.seat_boxes)
                 if cb.isChecked() and i < n]
        use_model = self.enable.isChecked() and bool(seats)
        provider = PROVIDERS[self.provider.currentIndex()][1]

        # 常见误填：把 API Key 粘进「API 地址」。这里就地纠错，
        # 否则后端会拼出 apikey_xxx/chat/completions 这种根本不是网址的地址。
        key = self.key.text().strip() or self.saved.get("JEV_API_KEY", "")
        base = self.base.text().strip() or self.saved.get("JEV_BASE_URL", "")
        model = self.model.text().strip() or self.saved.get("JEV_MODEL", "")
        corrected = False
        moved_key = False
        # apikey_ / tsf_ 只可能是 TypeSafe Jev 的 key：把「选了聊天网关 + deepseek 模型名」
        # 这种从别家配置抄过来的错配拉回自动识别，否则 Jev key 会被发到 /chat/completions。
        if key.startswith(("apikey_", "tsf_")):
            if provider != "auto":
                provider = "auto"
                corrected = True
            if model in DEEPSEEK_MODELS:
                model = ""
                corrected = True
                self.model.blockSignals(True)
                self.model.setText("")
                self.model.blockSignals(False)
        if looks_like_api_key(base):
            if not key:
                key = base
            base = ""
            if model in DEEPSEEK_MODELS:
                model = ""
            corrected = True
            moved_key = True
            self._relocate_pasted_key(key)
        invalid_base = bool(base) and not base.startswith(("http://", "https://"))

        args = ["--bots", str(n)]
        args += ["--memory", str(self.memory.value())]
        mode = MODES[self.mode.currentIndex()][1]
        if mode == "tournament":
            args += ["--mode", "tournament"]
            if self.blind_up.value() > 0:
                args += ["--blind-up", str(self.blind_up.value())]
        styles = {}
        for i in range(n):
            tag = STYLE_TAGS[self.seat_styles[i].currentIndex()]
            # 「随机」是后端的默认行为，不用写进命令行；只把写死的风格传下去
            if tag and tag != "随机":
                styles[i + 1] = tag
        if styles:
            args += ["--seat-styles",
                     ",".join(f"{k}={v}" for k, v in sorted(styles.items()))]
        if use_model:
            args += ["--llm-seats", ",".join(map(str, seats))]
            models = {}
            for i in range(n):
                text = self.seat_models[i].currentText().strip()
                if text and text != SEAT_FOLLOW:
                    models[i + 1] = text
            if models:
                args += ["--seat-models",
                         ",".join(f"{k}={v}" for k, v in sorted(models.items()))]
            # 纠错后一律回到自动识别，别把 Jev 的 key 强行发给聊天网关
            if provider != "auto" and not corrected:
                args += ["--provider", provider]
        else:
            args += ["--offline"]
        style = CARD_STYLES[self.cards.currentIndex()][1]
        if style != "cn":
            args += ["--cards", style]
        if not self.reasons.isChecked():
            args += ["--no-reasons"]
        for flag, edit, default in (("--sb", self.sb, "10"),
                                   ("--bb", self.bb, "20"),
                                   ("--stack", self.stack, "1000")):
            val = edit.text().strip()
            if val and val != default:
                args += [flag, val]

        env = os.environ.copy()
        for k in list(env):
            if k.startswith(("JEV_", "POKER_", "DEEPSEEK_")) and k != "POKER_BLUFF":
                env.pop(k, None)

        warn = ""
        if use_model:
            if key:
                env["JEV_API_KEY"] = key
            if base:
                env["JEV_BASE_URL"] = base
            if model:
                env["JEV_MODEL"] = model
            if moved_key:
                warn = "已自动纠正：API 地址里粘的是 Key · 已搬进 Key 框，改用官方 Jev 端点"
            elif corrected:
                warn = "已自动纠正：Jev 的 Key 不该配聊天网关 · 已改回自动识别"
            elif invalid_base:
                warn = "! API 地址不是网址（要 https:// 开头，别把 Key 填这里）"
            elif not key:
                warn = "提示：没检测到 API Key，在线座位会自动降级为本地 AI"
            elif provider == "openai" and not base:
                warn = "提示：聊天网关需要填 API 地址（以 /v1 结尾）"
            elif (model or "").strip().lower() in ("deepseek-chat", "deepseek-reasoner"):
                warn = f"! {model} 已于 2026-07-24 下线，模型名改成 deepseek-flash"
            else:
                warn = (f"在线座位 {','.join(map(str, seats))} · "
                        f"其余 {n - len(seats)} 个本地 AI")
        else:
            warn = "全部座位走本地 AI · 不联网、不产生 API 费用"
        # 状态栏只有一行，纠错/提示类信息更值得占位置，就不追加模式后缀了
        if mode == "tournament" and not warn.startswith(("!", "已自动纠正", "提示")):
            warn += " · 淘汰赛"
        return args, env, warn

    # ---------- 启动 ----------
    @staticmethod
    def powershell_cmd(args):
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", PLAY_PS1] + list(args)

    @staticmethod
    def game_cmd(args):
        """有 PokerGame.exe 就直接跑它（分享包/免 Python），否则走 play.ps1 + 源码。"""
        if os.path.isfile(GAME_EXE):
            return [GAME_EXE] + list(args)
        return Launcher.powershell_cmd(args)

    def _require_game(self):
        """单文件 exe 只带 UI；游戏本体在同目录，缺文件时给出可读提示。"""
        if os.path.isfile(GAME_EXE):
            return True
        if os.path.isfile(PLAY_PS1) and os.path.isfile(os.path.join(HERE, "poker.py")):
            return True
        self.status.setText("缺少 PokerGame.exe（或 play.ps1 + poker.py）：请把启动器放在游戏目录里")
        return False

    def launch_game(self):
        if not self._require_game():
            return
        args, env, warn = self.collect()
        try:
            subprocess.Popen(self.game_cmd(args), env=env, cwd=HERE,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except OSError as e:
            self.status.setText(f"启动失败：{e}")
            return
        self.status.setText("已启动游戏窗口 · " + warn)

    def launch_probe(self, flag, title):
        if not self._require_game():
            return
        args, env, _warn = self.collect()
        self.status.setText(f"正在执行{title} …")
        worker = ProbeWorker(self.game_cmd(args + [flag]), env)
        self.workers.append(worker)
        worker.done.connect(lambda out, t=title: self.show_output(t, out))
        worker.finished.connect(lambda: self.workers.remove(worker)
                                if worker in self.workers else None)
        worker.start()

    def show_output(self, title, out):
        self.status.setText(title + " 完成")
        OutputDialog(self, title, out).exec_()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")          # Fusion 对 QSS 圆角/自绘箭头的还原最稳定
    app.setFont(QFont("Microsoft YaHei UI", 9))
    app.setStyleSheet(styled_qss())

    # 隐藏诊断：PokerLauncher.exe --self-test，不开窗、写一份 exe-selftest.txt 就退出。
    if "--self-test" in sys.argv[1:]:
        win = Launcher()
        args, _env, warn = win.collect()
        lines = [
            f"frozen={IS_FROZEN}",
            f"exe={sys.executable}",
            f"app_dir={HERE}",
            f"play.ps1={os.path.isfile(PLAY_PS1)}",
            f"poker.py={os.path.isfile(os.path.join(HERE, 'poker.py'))}",
            f"config.bat={os.path.isfile(CONFIG_BAT)}",
            f"game.exe={os.path.isfile(GAME_EXE)}",
            f"key_saved={bool(win.saved.get('JEV_API_KEY'))}",
            f"qt_fonts={len(QFontDatabase().families())}",
            f"cmd={os.path.basename(win.game_cmd([])[0])} {' '.join(args)}",
            f"status={warn}",
        ]
        with open(os.path.join(HERE, "exe-selftest.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        sys.exit(0)

    win = Launcher()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    try:
        main()
    except Exception:                                          # noqa: BLE001
        with open(ERROR_LOG, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        raise
