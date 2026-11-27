"""固件中文字体覆盖回归测试。

设备上的子集字体只包含生成时指定的字符；标签文字一旦超出字符集，
LVGL 会渲染成「口」形豆腐块。本测试静态扫描 state_ui.c / turntable.c
中所有 make_label 与 lv_label_set_text 的字符串字面量，按标签实际
使用的字体（含运行时 fallback 组装）核对字形覆盖。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GENERATED = PROJECT_ROOT / "main" / "generated"
SOURCES = [PROJECT_ROOT / "main" / "state_ui.c", PROJECT_ROOT / "main" / "turntable.c",
           PROJECT_ROOT / "main" / "pomodoro.c"]

GLYPH_RE = re.compile(r'/\* U\+([0-9A-Fa-f]+) "(.)" \*/')
MAKE_LABEL_RE = re.compile(
    r'(?:(\w+)\s*=\s*)?make_label\(\s*[^,]+,\s*"((?:[^"\\]|\\.)*)"\s*,\s*&?\s*(\w+)')
SET_TEXT_RE = re.compile(r'lv_label_set_text\(\s*(\w+)\s*,(.*?)\);', re.DOTALL)
LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
FONT_ASSIGN_RE = re.compile(r'(\w+)\s*=\s*(ui_font_\w+)\s*;')
FALLBACK_RE = re.compile(r'(\w+)\.fallback\s*=\s*&(\w+)\s*;')


def _generated_glyphs() -> dict[str, set[str]]:
    fonts: dict[str, set[str]] = {}
    for path in GENERATED.glob("ui_font_*.c"):
        text = path.read_text(encoding="utf-8")
        fonts[path.stem] = {chr(int(code, 16)) for code, _ in GLYPH_RE.findall(text)}
    return fonts


def _is_cjk(character: str) -> bool:
    return ord(character) >= 0x2E80


class FirmwareFontCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fonts = _generated_glyphs()
        # LVGL 内置 montserrat 只有 ASCII；非 ASCII 字符一律视为缺字。
        self.builtin_ascii = {chr(code) for code in range(0x20, 0x7F)}
        self.aliases: dict[str, set[str]] = {}
        self.label_fonts: dict[str, str] = {}
        for source in SOURCES:
            text = source.read_text(encoding="utf-8")
            for var, font in FONT_ASSIGN_RE.findall(text):
                self.aliases[var] = set(self.fonts.get(font, set()))
            for var, fallback in FALLBACK_RE.findall(text):
                if var in self.aliases:
                    self.aliases[var] |= self.fonts.get(fallback, set())
            for var, _literal, font in MAKE_LABEL_RE.findall(text):
                if var:
                    self.label_fonts[var] = font

    def _glyphs_for(self, font: str) -> set[str] | None:
        if font in self.aliases:
            return self.aliases[font]
        if font in self.fonts:
            return self.fonts[font]
        if font.startswith("lv_font_montserrat"):
            return self.builtin_ascii
        return None

    def _check(self, text: str, font: str, where: str) -> None:
        glyphs = self._glyphs_for(font)
        if glyphs is None:
            return
        missing = [character for character in text
                   if _is_cjk(character) and character not in glyphs]
        self.assertFalse(
            missing,
            f"{where}: 字体 {font} 缺少字形 {''.join(missing)!r}（文字 {text!r}），设备上会显示为口口口")

    def test_make_label_literals_covered(self) -> None:
        for source in SOURCES:
            text = source.read_text(encoding="utf-8")
            for _var, literal, font in MAKE_LABEL_RE.findall(text):
                self._check(literal, font, f"{source.name} make_label")

    def test_set_text_literals_covered(self) -> None:
        for source in SOURCES:
            text = source.read_text(encoding="utf-8")
            for var, args in SET_TEXT_RE.findall(text):
                font = self.label_fonts.get(var)
                if font is None:
                    continue
                for literal in LITERAL_RE.findall(args):
                    self._check(literal, font, f"{source.name} lv_label_set_text({var})")

    def test_qoder_usage_kicker_covered(self) -> None:
        """用量页 kicker「额度已用」必须被其字体完整覆盖（真机豆腐块回归）。"""
        text = (PROJECT_ROOT / "main" / "state_ui.c").read_text(encoding="utf-8")
        body = text.split("static void build_qoder_usage(void)", 1)[1]
        match = re.search(r'"额度已用"\s*,\s*&?\s*(\w+)', body)
        self.assertIsNotNone(match, "未找到用量页 kicker 标签")
        self._check("额度已用", match.group(1), "build_qoder_usage kicker")


if __name__ == "__main__":
    unittest.main()