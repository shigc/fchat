"""离线 OCR 基座 core/ocr.py：引擎惰性单例、缺依赖降级、结果排序。

引擎用 fake 注入（不依赖真装了 rapidocr-onnxruntime），单测离线可跑。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ocr  # noqa: E402


def _box(x, y, w=40, h=10):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


class _FakeEngine:
    """模拟 RapidOCR：返回 (识别列表, 各阶段耗时列表)。"""

    def __init__(self, result=None, raises=None):
        self.result = result if result is not None else []
        self.raises = raises
        self.calls = []
        self.inside = 0
        self.overlap = False

    def __call__(self, path):
        self.calls.append(path)
        if self.raises is not None:
            raise self.raises
        # 串行锁被绕过时两个线程会同时进入
        self.inside += 1
        if self.inside > 1:
            self.overlap = True
        try:
            # 真引擎在「没识别到任何文字」时第一项返回 None
            out = list(self.result) if self.result is not None else None
            return out, [0.1, 0.2, 0.3]
        finally:
            self.inside -= 1


class RecognizeTest(unittest.TestCase):

    def setUp(self):
        self.engine = _FakeEngine()
        ocr._engine = self.engine
        self.img = self._write_image()

    def tearDown(self):
        ocr.reset_engine()
        try:
            os.remove(self.img)
        except OSError:
            pass

    @staticmethod
    def _write_image():
        path = os.path.join(os.environ.get("TEMP", "."), "ocr_probe.png")
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
        return path

    def test_lines_sorted_top_to_bottom_then_left(self):
        self.engine.result = [
            (_box(60, 100), "第二行", 0.9),
            (_box(20, 40), "第一行", 0.8),
            (_box(2, 40), "最左", 0.7),
        ]
        result = ocr.recognize(self.img)
        self.assertEqual([l.text for l in result.lines],
                         ["最左", "第一行", "第二行"])
        # 同一视觉行的两个框拼回一行；行距 6 倍行高 → 还原出一个空行；
        # 第二行起点 x=60、全局字宽 (40*3)/(4+6+6)=7.5px → 8 空格缩进
        self.assertEqual(result.text, "最左第一行\n\n        第二行")
        self.assertEqual(len(result), 3)

    def test_skips_empty_text_and_broken_items(self):
        self.engine.result = [
            (_box(0, 0), "   ", 0.5),
            (_box(0, 10), "有效", 0.9),
            ("不是三元组",),
            (_box(0, 20), None, 0.1),
        ]
        result = ocr.recognize(self.img)
        self.assertEqual(result.text, "有效")

    def test_none_result_is_empty(self):
        self.engine.result = None
        self.assertEqual(len(ocr.recognize(self.img)), 0)

    def test_engine_receives_path(self):
        ocr.recognize(self.img)
        self.assertEqual(self.engine.calls, [self.img])

    def test_missing_path_raises_ocr_error(self):
        with self.assertRaises(ocr.OcrError):
            ocr.recognize("")
        with self.assertRaises(ocr.OcrError):
            ocr.recognize(self.img + ".not-exist")

    def test_engine_runtime_error_becomes_ocr_error(self):
        self.engine.raises = RuntimeError("boom")
        with self.assertRaises(ocr.OcrError):
            ocr.recognize(self.img)

    def test_concurrent_calls_are_serialized(self):
        # 引擎被并发调用会让 onnxruntime 崩，靠锁串行；这里验证锁生效
        class _Slow(_FakeEngine):
            import time as _time

            def __call__(self, path):
                self._time.sleep(0.02)
                return super().__call__(path)

        engine = _Slow(result=[(_box(0, 0), "x", 0.9)])
        ocr._engine = engine
        threads = [threading.Thread(target=lambda: ocr.recognize(self.img))
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(engine.overlap)
        self.assertEqual(len(engine.calls), 3)


class EngineLifecycleTest(unittest.TestCase):

    def tearDown(self):
        ocr.reset_engine()

    def test_import_failure_raises_unavailable(self):
        with mock.patch.object(ocr, "_load_rapidocr",
                               side_effect=ImportError("依赖不可用")):
            with self.assertRaises(ocr.OcrUnavailable):
                ocr._get_engine()

    def test_model_load_failure_raises_unavailable(self):
        def _boom():
            raise OSError("模型文件缺失")

        with mock.patch.object(ocr, "_load_rapidocr", return_value=_boom):
            with self.assertRaises(ocr.OcrUnavailable):
                ocr._get_engine()

    def test_engine_is_created_once(self):
        built = []
        with mock.patch.object(
                ocr, "_load_rapidocr",
                return_value=lambda: built.append(1) or _FakeEngine()):
            first = ocr._get_engine()
            second = ocr._get_engine()
        self.assertIs(first, second)
        self.assertEqual(len(built), 1)

    def test_warmup_loads_engine(self):
        # 引擎导入走 _load_rapidocr 注入缝（不碰 sys.modules：预热定时器
        # 的并发真实 import 会被 import 机器按名覆盖/删除该键，冲掉
        # patch.dict 注入的 fake，套件实测 test_engine_is_created_once
        # 因此翻车）
        with mock.patch.object(ocr, "_load_rapidocr",
                               return_value=lambda: _FakeEngine()), \
                mock.patch.object(ocr, "is_available", return_value=True):
            self.assertTrue(ocr.warmup())
            self.assertIsNotNone(ocr._engine)
            # 预热后再识别不应重新加载
            self.assertTrue(ocr.warmup())

    def test_warmup_false_when_dep_missing(self):
        with mock.patch.object(ocr, "is_available", return_value=False):
            self.assertFalse(ocr.warmup())
        self.assertIsNone(ocr._engine)

    def test_warmup_swallows_load_failure(self):
        def _boom():
            raise OSError("模型文件缺失")

        with mock.patch.object(ocr, "_load_rapidocr", return_value=_boom), \
                mock.patch.object(ocr, "is_available", return_value=True):
            self.assertFalse(ocr.warmup())
        self.assertIsNone(ocr._engine)

    def test_reset_engine_clears_singleton(self):
        ocr._engine = _FakeEngine()
        ocr.reset_engine()
        self.assertIsNone(ocr._engine)

    def test_is_loaded_tracks_engine_singleton(self):
        """提示语据此区分「首次加载模型稍慢」，预热过就不该再提示。"""
        self.assertFalse(ocr.is_loaded())
        ocr._engine = _FakeEngine()
        self.assertTrue(ocr.is_loaded())
        ocr.reset_engine()
        self.assertFalse(ocr.is_loaded())


class LayoutFormatTest(unittest.TestCase):
    """版式还原：视觉行聚合、横向空格、段间空行、行首缩进。"""

    @staticmethod
    def _line(text, x, y, w, h=10.0):
        return ocr.OcrLine(text=text, x=x, y=y, w=w, h=h)

    def _text(self, *lines):
        return ocr.OcrResult(lines=list(lines)).text

    def test_horizontal_gap_becomes_spaces_cjk_aware(self):
        # 中文框：宽 80 / 显示宽 8（每字 2 倍宽）→ 半角当量 10px；
        # 30px 空隙 = 3 个空格
        text = self._text(self._line("文件名", 0, 0, 60, 10),
                          self._line("大小", 90, 0, 40, 10))
        self.assertEqual(text, "文件名   大小")

    def test_tight_gap_concatenates_without_space(self):
        text = self._text(self._line("ab", 0, 0, 20, 10),
                          self._line("cd", 21, 0, 20, 10))
        self.assertEqual(text, "abcd")

    def test_indent_reconstruction(self):
        # x=40、每半角字符 10px → 4 空格缩进
        text = self._text(self._line("缩进行", 40, 0, 60, 10))
        self.assertEqual(text, "    缩进行")

    def test_blank_line_between_paragraphs(self):
        text = self._text(self._line("上段", 0, 0, 20, 10),
                          self._line("下段", 0, 30, 20, 10))
        # 行距 30 - 行高 10 = 20 > 0.6*10
        self.assertEqual(text, "上段\n\n下段")

    def test_adjacent_rows_no_blank_line(self):
        text = self._text(self._line("第一行", 0, 0, 30, 10),
                          self._line("第二行", 0, 12, 30, 10))
        self.assertEqual(text, "第一行\n第二行")

    def test_row_split_by_y_overlap_not_absolute_y(self):
        # y 差 5px（字号内抖动）但垂直重叠充分，仍算同一行，按 x 排序；
        # 横向 50px 空隙按字宽还原成空格
        text = self._text(self._line("B", 60, 5, 10, 10),
                          self._line("A", 0, 0, 10, 10))
        self.assertEqual(text, "A     B")

    def test_global_char_width_ignores_sloppy_short_boxes(self):
        # 单字符框宽 15（自估字宽虚高），长框 60/6=10 才是真实字宽；
        # 30px 空隙按全局字宽算 3 空格，不随短框失真
        text = self._text(self._line("791569", 0, 0, 60, 10),
                          self._line("3", 90, 0, 15, 10))
        self.assertEqual(text, "791569   3")

    def test_tall_box_does_not_absorb_next_row(self):
        # 高框（h=20）与下一行矮框垂直重叠达标，但 y 中心差过远 → 不并行
        text = self._text(self._line("AAAA", 0, 0, 40, 20),
                          self._line("B", 0, 17, 10, 5))
        self.assertEqual(text, "AAAA\nB")

    def test_missing_size_degrades_to_simple_join(self):
        line = ocr.OcrLine(text="x", y=0)
        self.assertEqual(ocr.OcrResult(lines=[line]).text, "x")


class IsAvailableTest(unittest.TestCase):

    def test_true_when_spec_found(self):
        with mock.patch("importlib.util.find_spec", return_value=object()):
            self.assertTrue(ocr.is_available())

    def test_false_when_missing(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            self.assertFalse(ocr.is_available())

    def test_false_when_find_spec_raises(self):
        with mock.patch("importlib.util.find_spec",
                        side_effect=ImportError("no sys.path entry")):
            self.assertFalse(ocr.is_available())


class PreloadRuntimeTest(unittest.TestCase):
    """main._preload_msvc_runtime：PyQt5 捆的老 VC++ 运行库会弄崩
    onnxruntime 的 DLL 初始化，必须在导入 PyQt5 前预载系统运行库。"""

    def test_preload_is_safe_even_after_qt_loaded(self):
        import main
        main._preload_msvc_runtime()   # 不抛异常即可（缺运行库则跳过）

    def test_preload_call_precedes_pyqt5_import(self):
        # 载入顺序是功能成立的前提，源码顺序被挪到 PyQt5 之后即失效
        import main
        with open(main.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertLess(src.index("_preload_msvc_runtime()"),
                        src.index("from PyQt5"))


if __name__ == "__main__":
    unittest.main()
