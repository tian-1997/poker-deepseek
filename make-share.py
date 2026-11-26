"""Build the shareable package (no API key included).

Steps:
  1. build PokerLauncher.exe (GUI) and PokerGame.exe (console game) with PyInstaller
  2. assemble a clean folder with both exes + a blank config template + README
  3. verify no real API key leaked into any file (binary scan included)
  4. zip the folder

Run:  .venv\\Scripts\\python.exe make-share.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PYI = os.path.join(ROOT, ".venv", "Scripts", "pyinstaller.exe")
ICO = os.path.join(ROOT, "launcher.ico")

SHARE_PARENT = os.path.join(ROOT, "share")
SHARE_DIR = os.path.join(SHARE_PARENT, "poker-share")
ZIP_PATH = os.path.join(SHARE_PARENT, "德州扑克-分享版.zip")

SOURCE_FILES = ["cards.py", "engine.py", "deepseek.py", "jev.py", "memory.py",
                "preflop_table.py", "poker.py", "launcher.pyw", "play.ps1"]
EXTRA_FILES = ["PokerLauncher.exe", "PokerGame.exe", "launcher.ico",
               "poker-config.sample.bat"]

README = """德州扑克 · 单机命令行（你 + 最多 5 个 AI 同桌）
=====================================================

一、怎么开始
  1. 双击 PokerLauncher.exe（默认就是 5 个机器人 + 淘汰赛 + 纯本地 AI，直接点「启动游戏」就能玩）
  2. 想改人数就调「机器人数」（1 = 单挑，5 = 六人桌）
  3. 想改玩法就换「模式」：淘汰赛（见底出局，打到只剩一人）/ 现金局（筹码见底可以补）
  4. 想联网才去勾「启用在线模型」，并勾选哪几台机器走模型；
     每个座位还能单独选「风格」和「模型」
  5. 点「启动游戏」，会弹出一个控制台窗口，那就是牌桌

  不想联网、不花钱：保持「启用在线模型」不勾，全部座位走本地 AI（这是默认）。

  · 淘汰赛：谁的筹码见底就出局，会报「第 N 名」；最后剩下的那位是冠军。
    默认每 10 局盲注翻倍（可以在界面里调，0 = 不涨），不然短码相持会拖很久。
    真人也可能被淘汰——出局后本场结束，按 0 可以按同样配置再来一场。
  · 每座位风格：默认「随机」= 开局从风格池里抽（随机 / 稳健 / 激进 / 紧凶 / 松凶 /
    跟注站），一桌尽量不重样；也可以给某个座位写死一种打法。本地 AI 也认这套风格，
    会改变它的松紧和诈唬频率。
    局内看不到谁是什么风格，退出牌桌时才会揭晓（想照旧标出来加 --show-styles）。
  · 重开一场：局末按 0（或输「重开」）就按当前配置再来一场——筹码归位、座位全部回桌、
    从第 1 局重新数，抽到「随机」的座位会重新抽一次风格（写死的座位不变）。
    跨手记忆保留：它对你的印象不会因为重开而清空（想清空用 --forget）。
  · 本地 AI 会诈唬：有纯偷池、听牌半诈唬、上一街偷了下一街继续开第二枪，牌力很强时
    还会偶尔过牌设陷阱——所以「他一下注就说明牌好」不再成立。频率基数由风格决定
    （松凶最凶、跟注站几乎不偷），想整体调节用 --bluff 0~4 或环境变量 POKER_BLUFF。
  · 每座位模型：留「跟随全局」就用上面的全局模型；也可以填别的模型名，
    让一桌人分别用不同模型（比如一个 jev-latest、一个 gpt-4o-mini）互相对打。

二、要用 TypeSafe Jev（可选）
  - 到 https://console.typesafe.ai 申请自己的 key（apikey_ 开头）
  - 粘进「API Key」框即可，端点与模型名会自动填好，不用管
  - key 只随本次启动通过环境变量传给游戏，不写日志、不回显
  - 想每次都不用重粘：把 poker-config.sample.bat 复制成 poker-config.bat，
    在里面填上你自己的 key，启动器会自动读取

  本分享包内不含任何人的 API key，key 请自己申请、自己保管。
  费用由你的 key 所属账号承担；提前弃牌的局几乎不产生请求。

三、牌桌上怎么操作
  f 弃牌    c 跟注/过牌    r<金额> 加注    a 全下    x 一键清屏    ? 白话讲解
  一局结束：n 下一局    0 按当前配置重开一场    r 详细复盘    s 导出    q 退出

  默认中文牌面（方9 = 方块9，红A = 红桃A），局末自动亮底牌并列出每步下注理由。

  机器人会记住对手：默认记录最近 10 局的对手画像（入池率、翻前加注率、
  面对下注的弃牌率），Jev 和本地 AI 共用这份记忆，打得越久越会针对你。
  局末那一行「记忆(最近10局)」就是它们当前对你的判断。
  画像存在同目录 poker-memory.json；想重新被当成陌生人，删掉它或用 --forget。
  启动器里的「记忆局数」可调整窗口，填 0 = 关闭记忆（每局都失忆）。

  机器人怎么打：翻前按位置和起手牌强度表决定开池 / 3bet / 跟注 / 弃牌
  （72o 在枪口不会入池，K9o 不会去跟 3bet）；翻后按「对手这手的行动线」
  收缩他的范围再算胜率，不会再假设他随便拿两张牌。它下注的范围里始终有诈唬，
  所以不能只看它下注就弃牌。

四、电脑要求
  - Windows 10 / 11，无需安装 Python（游戏与启动器都已打包成 exe）
  - 第一次运行 Windows 可能提示「已保护你的电脑」，点「更多信息 → 仍要运行」
    （exe 没有购买代码签名证书，属于正常提示）

五、文件说明
  PokerLauncher.exe          图形启动器（双击这个）
  PokerGame.exe              游戏本体（由启动器调用，也可命令行直接跑）
  poker-config.sample.bat    配置模板（复制成 poker-config.bat 后填自己的 key）
  源码\\                      全部 Python 源码，方便自己改
"""


def run(cmd: list[str], cwd: str = ROOT) -> None:
    print(">", " ".join(cmd[:3]), "...")
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print((p.stdout or "")[-2000:])
        print((p.stderr or "")[-2000:])
        raise SystemExit(f"build failed: {p.returncode}")


def build_exe(script: str, name: str, windowed: bool, workdir: str) -> None:
    cmd = [PYI, "--noconfirm", "--clean", "--onefile",
           "--icon", ICO, "--name", name,
           "--distpath", ROOT, "--workpath", workdir, "--specpath", workdir,
           script]
    if windowed:
        cmd.insert(3, "--windowed")
    else:
        cmd.insert(3, "--console")
    run(cmd)


def real_key() -> str:
    """Read the author's own key only to prove it is NOT in the package."""
    path = os.path.join(ROOT, "poker-config.bat")
    if not os.path.isfile(path):
        return ""
    text = open(path, encoding="utf-8", errors="ignore").read()
    m = re.search(r'set\s+"?JEV_API_KEY=([^"\r\n]*)', text)
    return (m.group(1).strip() if m else "")


FAKE_MARKERS = ("fake", "example", "placeholder", "your_key", "yourkey",
                "xxx", "dummy", "sample", "test")


def scan_leaks(key: str) -> list[str]:
    problems = []
    key_bytes = key.encode()
    for base, _dirs, files in os.walk(SHARE_DIR):
        for fn in files:
            path = os.path.join(base, fn)
            data = open(path, "rb").read()
            rel = os.path.relpath(path, SHARE_DIR)
            if key and key_bytes in data:
                problems.append(f"real key found in {rel}")
            if fn.lower().endswith((".txt", ".bat", ".py", ".ps1", ".md", ".json")):
                text = data.decode("utf-8", errors="ignore")
                for hit in re.findall(r"(?:apikey_|sk-)[A-Za-z0-9_\-]{16,}", text):
                    if any(m in hit.lower() for m in FAKE_MARKERS):
                        continue      # 文档/示例里的假 key，不算泄漏
                    problems.append(f"key-like string in {rel}: {hit[:12]}...")
    return problems


def main() -> int:
    if not os.path.isfile(PYI):
        print("pyinstaller not found in .venv; run: uv pip install --python "
              ".venv/Scripts/python.exe pyinstaller")
        return 1
    if not os.path.isfile(ICO):
        print("launcher.ico missing")
        return 1

    skip_build = "--skip-build" in sys.argv[1:]
    if skip_build:
        print("[1-2/4] skip build (reuse existing exes)")
    else:
        print("[1/4] build PokerLauncher.exe")
        build_exe("launcher.pyw", "PokerLauncher", True, os.path.join(ROOT, "_build_launcher"))
        print("[2/4] build PokerGame.exe")
        build_exe("poker.py", "PokerGame", False, os.path.join(ROOT, "_build_game"))

    print("[3/4] assemble share folder")
    if os.path.isdir(SHARE_DIR):
        # 有些环境（回收站不可用 / exe 被占用）删不掉旧目录；就地覆盖也能得到正确的包，
        # 因为下面的文件清单是固定的，不会残留旧文件。
        try:
            shutil.rmtree(SHARE_DIR)
        except OSError as e:
            print(f"  ! 旧目录没删掉（{type(e).__name__}），改为就地覆盖")
    os.makedirs(os.path.join(SHARE_DIR, "源码"), exist_ok=True)
    for fn in EXTRA_FILES:
        src = os.path.join(ROOT, fn)
        if not os.path.isfile(src):
            raise SystemExit(f"missing {fn}")
        shutil.copy2(src, os.path.join(SHARE_DIR, fn))
    for fn in SOURCE_FILES:
        src = os.path.join(ROOT, fn)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(SHARE_DIR, "源码", fn))
    with open(os.path.join(SHARE_DIR, "使用说明.txt"), "w",
              encoding="utf-8-sig", newline="\r\n") as f:
        f.write(README)

    print("[4/4] leak scan + zip")
    problems = scan_leaks(real_key())
    if problems:
        for p in problems:
            print("  LEAK:", p)
        raise SystemExit("aborted: package contains an API key")
    print("  no api key inside the package")

    if os.path.isfile(ZIP_PATH):
        os.remove(ZIP_PATH)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(SHARE_DIR):
            for fn in files:
                path = os.path.join(base, fn)
                z.write(path, os.path.join("poker-share",
                                           os.path.relpath(path, SHARE_DIR)))
    size = os.path.getsize(ZIP_PATH) / 1024 / 1024
    print(f"done: {ZIP_PATH} ({size:.1f} MB)")
    print(f"folder: {SHARE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
