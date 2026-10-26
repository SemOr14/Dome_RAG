"""Real DOM regression for polling, node scroll and automatically promoted charts.

Optional browser dependency: uv pip install playwright; uses system Chromium when available.
"""
import shutil
import socket
import threading
from pathlib import Path

import pytest
import uvicorn

from integration.server import create_app
from llm_agent.storage.data_api import envelope


def test_polling_preserves_node_details_scroll_and_highlights_nondefault_chart(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    app = create_app(tmp_path / 'browser.sqlite3', token='')
    run = app.state.hub.run_id
    def append(stamp, pressure=10):
        nodes = {'mystery': {'updated_at': stamp, 'online': True, 'status': 'OK', 'pressure': pressure,
                            'diagnostic_dump': {f'row{i}': 'Пример поля '+str(i) for i in range(80)}}}
        app.state.store.append([envelope(run, 'telemetry', {'type': 'telemetry', 'timestamp': stamp, 'data': {'nodes': nodes}},
                               session_id='ui-test', source='dome', channel='websocket', direction='inbound')])
    for i in range(12):
        append(i)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            executable = shutil.which('chromium') or p.chromium.executable_path
            if not Path(executable).exists():
                pytest.skip('Chromium is not installed')
            browser = p.chromium.launch(executable_path=executable, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{port}/ui/')
            page.locator('.node-card summary').click()
            page.locator('.node-card pre').evaluate('(pre)=>{pre.scrollTop=250;pre.scrollLeft=5;window.originalPre=pre;}')
            # Update through the actual polling function, without taking focus away from the node.
            append(12)
            page.evaluate('refresh()')
            assert page.locator('.node-card details').evaluate('(el)=>el.open')
            assert page.locator('.node-card pre').evaluate('(el)=>el===window.originalPre && el.scrollTop===250')
            page.locator('nav button[data-page="charts"]').click()
            page.wait_for_function('document.querySelectorAll("#charts canvas").length===20')
            append(13, pressure=90)
            page.evaluate('refresh()')
            page.wait_for_function('document.querySelectorAll("#charts canvas").length===21')
            first = page.locator('#charts > article').first
            assert 'chart-anomaly' in first.get_attribute('class')
            assert first.locator('.chart-title').inner_text() == 'mystery · pressure'
            assert first.evaluate('(el)=>getComputedStyle(el).borderTopColor') == 'rgb(255, 240, 0)'
            page.locator('nav button[data-page="overview"]').click()
            assert page.locator('.node-card details').evaluate('(el)=>el.open')
            assert page.locator('.node-card pre').evaluate('(el)=>el===window.originalPre && el.scrollTop===250')
            assert not errors
            assert app.state.hub.client is None
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
