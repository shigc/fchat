#!/usr/bin/env python3
"""Fchat 一键发布构建：Cython 编译 .pyd → PyInstaller onedir → Inno Setup → 上传。

流程（每步失败即中止）：
  1. 环境自检（gcc / Cython / Inno Setup，缺啥装啥；Inno 需 winget）
  2. staging 组装：core/ ui/ config.py 全部 .py → Cython 生成 .c →
     gcc 直接链 python312.dll 编成 .pyd（staging 内不留任何明文 .py）；
     .env 的 FCHAT_UPDATE_BASE_URL 生成 _update_cfg.pyd（更新地址烧进
     机器码，.env 明文不进包）
  3. 以 staging 为 PYTHONPATH 跑全量 unittest 冒烟（验证 .pyd 可导入、测试全绿）
  4. PyInstaller 打包（spec 在第 2 步动态生成到 staging，入口 staging/main.py，
     modulegraph 只见 .pyd 不见源码）
  5. 断言 dist 内无明文 .py / update.yaml
  6. Inno Setup 生成安装包
  7. 计算 sha256 生成 manifest.json
  8. 上传安装包 + manifest 到 nginx 静态目录（FCHAT_UPLOAD_DIR）

用法：python build_release.py
配置：项目根 .env（见 .env.example），也可用同名环境变量覆盖
  FCHAT_GCC=gcc 路径；FCHAT_UPLOAD_DIR=上传目录；FCHAT_SKIP_TESTS=1 跳过冒烟
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(ROOT, "build")
STAGING = os.path.join(BUILD, "staging")
CYTHON_OUT = os.path.join(BUILD, "cython")
DIST = os.path.join(BUILD, "dist")
OUTPUT = os.path.join(BUILD, "output")

# 配置统一从项目根 .env 读取（模板见 .env.example），也允许环境变量覆盖。
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:  # 未装 python-dotenv 时仅靠环境变量，不影响构建
    pass

UPLOAD_DIR = os.environ.get("FCHAT_UPLOAD_DIR") or ""

sys.path.insert(0, ROOT)
from core.version import APP_VERSION  # noqa: E402

GCC = os.environ.get("FCHAT_GCC") or ""


def run(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd))
    r = subprocess.run([str(c) for c in cmd], **kw)
    if r.returncode != 0:
        sys.exit(f"命令失败（exit {r.returncode}）：{' '.join(str(c) for c in cmd)}")
    return r


def step1_env():
    global GCC
    if not os.path.isfile(GCC):
        found = shutil.which("gcc")
        if not found:
            sys.exit("找不到 gcc，请设置 .env 的 FCHAT_GCC")
        GCC = found
    run([GCC, "--version"], stdout=subprocess.DEVNULL)
    try:
        import Cython  # noqa: F401
    except ImportError:
        run([sys.executable, "-m", "pip", "install", "cython>=3.0"])
    iscc = find_iscc()
    if not iscc:
        if os.environ.get("FCHAT_SKIP_INNO"):
            print("[1/8] 未找到 Inno Setup，FCHAT_SKIP_INNO=1，只跑到打包校验")
            return None
        print("未找到 Inno Setup，尝试 winget 安装…")
        run(["winget", "install", "-e", "--id", "JRSoftware.InnoSetup",
             "--silent", "--accept-package-agreements",
             "--accept-source-agreements"])
        iscc = find_iscc()
        if not iscc:
            sys.exit("Inno Setup 安装失败，请手动安装后重跑")
    print(f"[1/8] 环境就绪：gcc={GCC}")
    return iscc


def find_iscc():
    cand = [
        shutil.which("ISCC"),
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        r"D:\Program Files\Inno Setup 7\ISCC.exe",
        r"C:\Program Files\Inno Setup 7\ISCC.exe",
    ]
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def collect_hiddenimports(files):
    """AST 静态扫描全部源码的 import，生成 PyInstaller hiddenimports。

    业务代码全部编成 .pyd 后 modulegraph 看不进内部导入（曾导致打包版
    启动报 No module named 'logging.handlers'），必须把每个模块 import
    到的包/子模块显式列全。ast.ImportFrom 的属性名可能是类/函数而非
    模块，用 find_spec 过滤掉不存在的名字，避免 spec 里堆无效项。
    """
    mods = set()
    for path in files:
        rel = os.path.relpath(path, ROOT)
        pkg = os.path.dirname(rel).replace(os.sep, ".")
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    mods.add(node.module)
                    for a in node.names:
                        if a.name != "*":
                            mods.add(f"{node.module}.{a.name}")
                elif node.level > 0 and pkg:
                    full = f"{pkg}.{node.module}" if node.module else pkg
                    mods.add(full)
                    for a in node.names:
                        if a.name != "*":
                            mods.add(f"{full}.{a.name}")
    mods.discard("__future__")
    real = []
    for m in sorted(mods):
        try:
            if importlib.util.find_spec(m) is not None:
                real.append(m)
        except (ImportError, ValueError):
            # 属性名（类/函数）或条件导入的目标不存在：不是模块，跳过
            continue
    return real


def python_dll() -> str:
    name = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    for d in (sys.base_prefix, os.path.join(sys.base_prefix, "DLLs")):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    sys.exit(f"找不到 {name}")


def step2_staging():
    if os.path.isdir(STAGING):
        shutil.rmtree(STAGING)
    if os.path.isdir(CYTHON_OUT):
        shutil.rmtree(CYTHON_OUT)
    os.makedirs(STAGING)

    # .env 的更新地址 → _update_cfg.py（随后与其它模块一样编译成 .pyd）
    base_url = (os.environ.get("FCHAT_UPDATE_BASE_URL") or "").strip()
    manifest = (os.environ.get("FCHAT_UPDATE_MANIFEST") or "").strip()
    with open(os.path.join(ROOT, "_update_cfg.py"), "w", encoding="utf-8") as f:
        f.write('"""打包时由 build_release.py 从 .env 生成，勿手改。"""\n')
        f.write(f'BASE_URL = {base_url!r}\n')
        f.write(f'MANIFEST = {manifest or "manifest.json"!r}\n')

    # 待编译模块：core/ ui/ 全部 .py + 根下 config.py + 烧录配置。
    sources = []
    for pkg in ("core", "ui"):
        pkg_dir = os.path.join(ROOT, pkg)
        for cur, dirs, files in os.walk(pkg_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    sources.append(os.path.join(cur, name))
    sources.append(os.path.join(ROOT, "config.py"))
    sources.append(os.path.join(ROOT, "_update_cfg.py"))

    dll = python_dll()
    include = sysconfig.get_paths()["include"]

    def _build_one(src: str):
        """单个模块：cython 生成 .c → gcc 编 .pyd。工作全在子进程，
        线程池只是并发发命令，IO/CPU 都由子进程吃满多核。"""
        rel = os.path.relpath(src, ROOT)
        base = os.path.splitext(rel)[0]
        c_file = os.path.join(CYTHON_OUT, base + ".c")
        os.makedirs(os.path.dirname(c_file), exist_ok=True)
        pyd = os.path.join(STAGING, base + ".pyd")
        os.makedirs(os.path.dirname(pyd), exist_ok=True)
        # annotation_typing=False：注解退化为普通对象。否则 Cython 会
        # 强制参数类型（如 x: str 收到 None 即 TypeError），源码模式注解
        # 本来就不校验，编译版不能更严格
        run([sys.executable, "-m", "cython", "-3",
             "-X", "annotation_typing=False", src, "-o", c_file])
        # ld 直接把 python312.dll 当输入文件自建导入表；-static-libgcc
        # 避免运行时依赖 libgcc_s_seh-1.dll
        run([GCC, "-O2", "-shared", "-static-libgcc",
             "-I", include, "-o", pyd, c_file, dll])
        if not os.path.isfile(pyd):
            raise RuntimeError(f"编译失败：{rel}")
        return rel

    # cython 是纯 Python（CPU 密集）、gcc 独立进程，按核数并发
    workers = min(32, (os.cpu_count() or 4) + 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rel in pool.map(_build_one, sources):
            print(f"  .pyd {rel}")
    os.remove(os.path.join(ROOT, "_update_cfg.py"))

    # 入口与资源（style.qss 无引用但保留无害；明确不带 update.yaml 明文）
    shutil.copy2(os.path.join(ROOT, "main.py"),
                 os.path.join(STAGING, "main.py"))
    shutil.copytree(os.path.join(ROOT, "resources"),
                    os.path.join(STAGING, "resources"))

    # hiddenimports：扫描全部源码（含入口 main.py）的 import。
    # modulegraph 看不进 .pyd，缺一个就启动报 ModuleNotFoundError
    # （_update_cfg.py 已在上面编译完删掉，且只有常量无 import，不参与扫描）
    hidden = collect_hiddenimports(
        [s for s in sources
         if s != os.path.join(ROOT, "_update_cfg.py")]
        + [os.path.join(ROOT, "main.py")])
    # _update_cfg 只被 updater 的 frozen 分支动态导入，modulegraph
    # 看不进 .pyd，AST 扫描又排除了它，必须在这里显式补上，否则打包版
    # 读不到烧录的更新地址（表现为"未配置更新服务器"）
    hidden.append("_update_cfg")
    print(f"hiddenimports 共 {len(hidden)} 项（AST 扫描生成）")

    # 动态生成 spec 到 staging：modulegraph 的搜索路径只含 staging，
    # 源码根不进搜索路径，从根上杜绝 .py 被收进包
    spec = SPEC_TEMPLATE.format(
        staging=STAGING, root=ROOT, app_version=APP_VERSION,
        hiddenimports=hidden)
    with open(os.path.join(STAGING, "main.spec"), "w", encoding="utf-8") as f:
        f.write(spec)
    print(f"[2/8] staging 组装完成：{len(sources)} 个模块编译为 .pyd")


def step3_smoke():
    # 导入检查必跑（几秒钟）：逐个导入 staging 的 .pyd，能在出包前
    # 抓住模块初始化期崩溃（如 Cython 校验注解默认值抛 TypeError）。
    # -P 阻止 python 把 cwd 插到 sys.path 前面——否则仓库根的源码 .py
    # 会遮蔽 staging 的 .pyd，检查就白做了
    mods = []
    for cur, _dirs, files in os.walk(STAGING):
        for name in files:
            if name.endswith(".pyd"):
                rel = os.path.relpath(os.path.join(cur, name), STAGING)
                mods.append(os.path.splitext(rel)[0].replace(os.sep, "."))
    code = ("import importlib\n"
            "for m in %r:\n"
            "    importlib.import_module(m)" % sorted(mods))
    run([sys.executable, "-P", "-c", code], cwd=STAGING,
        env={**os.environ, "PYTHONPATH": STAGING})
    print(f"[3/8] 导入检查：{len(mods)} 个 .pyd 全部可导入")

    if os.environ.get("FCHAT_SKIP_TESTS"):
        print("[3/8] 跳过全量单测（FCHAT_SKIP_TESTS=1）")
        return
    env = dict(os.environ)
    env["PYTHONPATH"] = STAGING
    # 用 staging 的 .pyd 跑全量单测：既能验证扩展模块可导入，
    # 也保证打包的就是测试过的那份代码。-P 见上，理由相同。
    # 但 tests/*.py 每个文件都把「自身上一级目录」插到 sys.path[0]，
    # 直接在仓库根 discover 会让那个 insert 指向仓库根，源码 .py 反过来
    # 遮蔽 staging 的 .pyd（1.3.x 的 Cython NULL 误编译因此漏检出包）。
    # 把 tests 复制进 staging 再跑，insert 才落在 .pyd 上。
    tests_staging = os.path.join(STAGING, "tests")
    shutil.rmtree(tests_staging, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "tests"), tests_staging,
                    ignore=shutil.ignore_patterns("__pycache__"))
    try:
        run([sys.executable, "-P", "-m", "unittest", "discover", "-s", "tests"],
            cwd=STAGING, env=env)
    finally:
        # 不给 PyInstaller 留下明文测试源码，也维持 staging「无业务 .py」
        shutil.rmtree(tests_staging, ignore_errors=True)
    print("[3/8] 全量单测（.pyd）通过")


def step4_pyinstaller():
    # distpath/workpath 显式给绝对路径：PyInstaller 相对 CWD 解析，
    # 而 spec 从 staging 目录执行，不指明会落到 build/staging/dist
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", DIST, "--workpath", os.path.join(BUILD, "work"),
         os.path.join(STAGING, "main.spec")],
        cwd=STAGING, env={**os.environ, "PYTHONPATH": STAGING})
    print("[4/8] PyInstaller 完成")


def step5_assert():
    app_dir = os.path.join(DIST, "fchat")
    if not os.path.isdir(app_dir):
        sys.exit("dist/fchat 不存在，打包失败")
    # 只校验「本项目源码」是否以明文混入：config.py、_update_cfg.py、main.py
    # 与 core/、ui/ 下的模块。这些在 staging 里都编译成了 .pyd，若 dist 里
    # 出现对应的 .py/.pyc 即源码保护失败。第三方依赖（cv2 等）自带的 .py
    # 属合法依赖文件，不在校验范围（PyInstaller 6.x 全收进 _internal）。
    protected = ("config", "_update_cfg", "main")
    bad = []
    for cur, _dirs, files in os.walk(app_dir):
        rel = os.path.relpath(cur, app_dir).replace(os.sep, "/")
        for name in files:
            if name in ("update.yaml", ".env"):
                bad.append(os.path.join(cur, name))
                continue
            stem, ext = os.path.splitext(name)
            if ext not in (".py", ".pyc"):
                continue
            # 仅在顶层或 core/、ui/ 下才算本项目源码
            if rel == "." and stem in protected:
                bad.append(os.path.join(cur, name))
            elif rel in ("core", "ui"):
                bad.append(os.path.join(cur, name))
    if bad:
        sys.exit("发现明文文件混入 dist（源码保护失败）：\n" + "\n".join(bad))
    print("[5/8] dist 内无本项目明文 .py/.pyc/update.yaml/.env，源码保护校验通过")


def step6_inno(iscc: str) -> str:
    run([iscc, f"/DAPP_VERSION={APP_VERSION}",
         os.path.join(ROOT, "packaging", "fchat.iss")])
    exe = os.path.join(OUTPUT, f"Fchat-{APP_VERSION}-setup.exe")
    if not os.path.isfile(exe):
        sys.exit(f"安装包未生成：{exe}")
    print(f"[6/8] 安装包：{exe}")
    return exe


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_release_notes() -> list:
    """本次发版更新说明：优先 FCHAT_NOTES 环境变量（\n 分隔），
    否则从 RELEASE_NOTES.md 提取最新版本段的说明行。返回字符串列表。

    RELEASE_NOTES.md 以 '## 版本号' 为版本标题，取第一个标题之后、
    下一个标题之前的正文行作为最新版本说明；每行去掉 '- '/'· ' 前缀，
    过滤注释与空行。无内容返回空列表（manifest 不带 notes 字段）。
    """
    raw = os.environ.get("FCHAT_NOTES")
    if raw is not None:
        return [ln.strip() for ln in raw.splitlines() if ln.strip()]
    path = os.path.join(ROOT, "RELEASE_NOTES.md")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    starts = [i for i, ln in enumerate(lines)
              if re.match(r"^\s*##\s+", ln)]
    if not starts:
        return []
    begin, end = starts[0] + 1, (starts[1] if len(starts) > 1 else len(lines))
    out = []
    for ln in lines[begin:end]:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        for prefix in ("- ", "· ", "* ", "• "):
            if ln.startswith(prefix):
                ln = ln[len(prefix):]
                break
        out.append(ln)
    return out


def step7_manifest(setup_exe: str) -> str:
    base = (os.environ.get("FCHAT_UPDATE_BASE_URL") or "").strip().rstrip("/")
    name = os.path.basename(setup_exe)
    manifest = {
        "version": APP_VERSION,
        "url": f"{base}/{name}",
        "sha256": sha256_of(setup_exe),
        "released_at": __import__("datetime").datetime.now().isoformat(
            timespec="seconds"),
    }
    notes = load_release_notes()
    if notes:
        manifest["notes"] = notes
    out = os.path.join(OUTPUT, "manifest.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[7/8] manifest：{out}")
    if notes:
        print(f"      更新说明 {len(notes)} 条：")
        for n in notes:
            print(f"        - {n}")
    return out


def step8_upload(setup_exe: str, manifest: str):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    for src in (setup_exe, manifest):
        shutil.copy2(src, os.path.join(UPLOAD_DIR, os.path.basename(src)))
    print(f"[8/8] 已上传到 {UPLOAD_DIR}")


SPEC_TEMPLATE = """# -*- mode: python ; coding: utf-8 -*-
# 由 build_release.py 生成，勿手改。入口在 staging（纯 .pyd），modulegraph
# 看不进 .pyd 内部导入，hiddenimports 必须显式列全第三方依赖。
import os

STAGING = r"{staging}"
ROOT = r"{root}"

a = Analysis(
    [os.path.join(STAGING, "main.py")],
    pathex=[STAGING],
    binaries=[],
    datas=[(os.path.join(ROOT, "resources"), "resources")],
    hiddenimports={hiddenimports!r},
    hookspath=[],
    runtime_hooks=[],
    excludes=["pydoc_data"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="fchat",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(ROOT, "resources", "fchat.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="fchat",
)
"""


def main():
    iscc = step1_env()
    step2_staging()
    step3_smoke()
    step4_pyinstaller()
    step5_assert()
    if not iscc:
        print("\n打包校验完成（未生成安装包）：安装 Inno Setup 后重跑即可出包")
        return
    exe = step6_inno(iscc)
    manifest = step7_manifest(exe)
    step8_upload(exe, manifest)
    print(f"\n完成：Fchat {APP_VERSION} 已发布到 {UPLOAD_DIR}")


if __name__ == "__main__":
    main()
