"""数据目录（缓存/日志）唯一来源。

config.yaml 始终留在系统默认配置目录，不随存储路径迁移（密钥存系统 keyring，
不再有 secret.key 文件）；可迁移的只有 cache/ 与 logs/。存储根目录默认与
CONFIG_DIR 一致，可被 config 的 storage_dir 字段覆盖。migrate_storage 采用
「先完整复制 → 校验 → 写配置回读确认 → 再切换」的顺序，整体失败（如目标不可写、
校验不一致、配置写入失败）时配置不动；个别文件被占用则跳过并告警（旧目录仍保留该文件）。
旧目录数据永远不自动删除。
"""
from __future__ import annotations

import logging
import os
import shutil

log = logging.getLogger(__name__)

# 懒加载标记：storage_root() 首次调用时读一次配置，之后用缓存值
_current_root = ""
_root_loaded = False


def storage_root() -> str:
    """当前存储根目录；首次调用时从配置懒加载。"""
    global _current_root, _root_loaded
    if not _root_loaded:
        _root_loaded = True
        from config import CONFIG_DIR, load_config
        _current_root = CONFIG_DIR
        try:
            saved = load_config().get("storage_dir", "")
            if saved and os.path.isdir(saved):
                _current_root = saved
        except Exception as e:  # noqa: BLE001 —— 配置损坏退回默认目录
            log.warning("读取存储路径配置失败，使用默认目录：%s", e)
    return _current_root


def cache_root() -> str:
    return os.path.join(storage_root(), "cache")


def avatars_dir() -> str:
    return os.path.join(cache_root(), "avatars")


def images_dir() -> str:
    return os.path.join(cache_root(), "images")


def files_dir() -> str:
    return os.path.join(cache_root(), "files")


def logs_dir() -> str:
    return os.path.join(storage_root(), "logs")


def _set_root(path: str):
    global _current_root, _root_loaded
    _current_root = path
    _root_loaded = True


def _copy_tree(src: str, dst: str) -> set:
    """递归复制目录。单个文件失败（被占用等）跳过并告警，不中断整体迁移。

    返回被跳过的源文件集合：这些文件没复制过去，校验时不能按"目标缺失"
    判失败（源被占用是文档允许的容忍情形，旧目录仍保留该文件）。
    """
    skipped = set()
    for cur, _dirs, files in os.walk(src):
        rel = os.path.relpath(cur, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for name in files:
            s = os.path.join(cur, name)
            d = os.path.join(target, name)
            try:
                shutil.copy2(s, d)
            except OSError as e:
                log.warning("迁移跳过 %s：%s", s, e)
                skipped.add(s)
    return skipped


def _same_file(s: str, d: str) -> bool:
    """目标侧文件是否存在且与源端大小一致；读不到一律算不一致。"""
    try:
        return os.path.exists(d) and os.path.getsize(d) == os.path.getsize(s)
    except OSError:
        return False


# skipped 不写 set 注解：Cython 把注解当类型声明并校验默认值，
# frozenset() 不是 set 实例，编译版模块导入即抛 TypeError
def _verify_tree(src: str, dst: str, skipped=frozenset()):
    """校验目标侧文件齐全（大小一致），漏掉的重试一次（多为复制时被占用）。

    校验是迁移的闸门：重试后仍缺失/大小不一致就抛 OSError，让
    migrate_storage 在写配置之前中止（配置不动、旧目录数据原样可用）。
    此前只记日志继续跑，配置切过去后文件实际缺在旧目录，等于数据丢失。
    skipped 里的文件源端就没复制成功，不在校验要求内。
    """
    bad = 0
    for cur, _dirs, files in os.walk(src):
        rel = os.path.relpath(cur, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        for name in files:
            s = os.path.join(cur, name)
            if s in skipped:
                continue
            d = os.path.join(target, name)
            if _same_file(s, d):
                continue
            try:
                shutil.copy2(s, d)
                log.info("迁移补拷 %s", s)
            except OSError as e:
                log.warning("迁移校验补拷失败（文件保留在旧目录）%s：%s", s, e)
            if not _same_file(s, d):
                bad += 1
    if bad:
        raise OSError(f"迁移校验失败：{bad} 个文件不一致，已放弃切换")


def migrate_storage(new_root: str) -> str:
    """把 cache/ 与 logs/ 复制到新路径，成功后写配置并热切换。

    返回已迁移的子目录名（如 "cache、logs"）。失败抛异常，配置保持不变。
    """
    # 空路径必须先于 abspath 校验：abspath("") 会返回当前工作目录，
    # 之后再判非空永远为真，空串会被静默迁移到 CWD
    if not (new_root or "").strip():
        raise ValueError("新路径无效或与当前路径相同")
    new_root = os.path.abspath(new_root)
    if os.path.normcase(new_root) == os.path.normcase(storage_root()):
        raise ValueError("新路径无效或与当前路径相同")
    # 新路径不得落在当前 cache/logs 目录树内（否则新存储根嵌进旧树，
    # "旧目录保留兜底"会把正在使用的新存储一起删掉）。realpath 归一
    # 覆盖别名；commonpath 跨盘符会抛 ValueError，视为"不在树内"。
    new_real = os.path.realpath(new_root)
    for sub in (cache_root(), logs_dir()):
        sub_real = os.path.realpath(sub)
        try:
            inside = os.path.commonpath([new_real, sub_real]) == sub_real
        except ValueError:
            inside = False
        if inside:
            raise ValueError("新路径不能位于当前缓存/日志目录内")
    # 目标不可写（盘满/无权限/是文件）时在这里就失败，配置还没动
    os.makedirs(new_root, exist_ok=True)
    moved = []
    for sub in ("cache", "logs"):
        src = os.path.join(storage_root(), sub)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(new_root, sub)
        skipped = _copy_tree(src, dst)
        _verify_tree(src, dst, skipped)
        moved.append(sub)
    from config import save_storage_dir, load_config
    save_storage_dir(new_root)
    # save_config 按惯例吞掉写盘失败：必须回读确认，否则迁移"成功"
    # 但重启后静默回退旧目录，期间缓存/日志分叉
    if os.path.normcase(load_config().get("storage_dir", "")) != os.path.normcase(new_root):
        raise OSError("存储路径写入配置失败，迁移中止")
    _set_root(new_root)
    log.info("存储路径已迁移到 %s（%s）", new_root, "、".join(moved) or "无数据")
    return "、".join(moved) if moved else "无数据"


def clear_cache():
    """清空 cache/ 下全部内容（不动 logs/）。被占用文件跳过。"""
    root = cache_root()
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        p = os.path.join(root, name)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        except OSError as e:
            log.warning("清理缓存跳过 %s：%s", p, e)


def dir_size(path: str) -> int:
    """递归统计目录字节数；不可读文件跳过。供后台线程调用。"""
    total = 0
    for cur, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(cur, name))
            except OSError:
                pass
    return total
