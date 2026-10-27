"""Веб-панель в настоящем браузере: вкладки «События» и «Узлы», сохранение раскрытых блоков, отправка воздействий.

Нужен Chrome, Chromium или Edge; без них тест пропускается. Сеть и Купол не используются (fetch подменён),
каталог узлов — настоящий фрагмент ответа describe_nodes (tests/fixtures).
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Модуль подключается по пути: пакет `tests` общий с agent/tests, и импорт `tests.browser_panel_harness` ломает поиск tests.dome_fake
sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_panel_harness import build  # noqa: E402

CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
)


def browser() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        if found := shutil.which(name):
            return found
    return next((path for path in CANDIDATES if os.path.exists(path)), None)


@pytest.mark.skipif(browser() is None, reason="нужен Chrome, Chromium или Edge")
@pytest.mark.parametrize("profile,minimum", [("service", 30), ("competition", 26)])
def test_panel_tabs_keep_open_blocks_and_send_controls(tmp_path, profile, minimum):
    page = build(tmp_path / f"{profile}.html", profile)
    out = subprocess.run(
        [browser(), "--headless=new", "--disable-gpu", "--no-sandbox", "--allow-file-access-from-files",
         f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=30000", "--dump-dom", page.as_uri()],
        capture_output=True, text=True, timeout=120, encoding="utf-8",
    ).stdout
    match = re.search(r'<pre id="result">(.*?)</pre>', out, re.S)
    assert match, "страница не вернула результат проверок"
    result = json.loads(html.unescape(match.group(1)))
    assert result["failed"] == []
    assert result["passed"] >= minimum
