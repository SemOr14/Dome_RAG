"""Single HTTP/runtime process; only launched by integration.launcher."""
import os
import uvicorn


def main():
    port = int(os.getenv('PANEL_PORT', '8080'))
    os.environ.setdefault('DATA_API_URL', f'http://127.0.0.1:{port}')
    uvicorn.run('integration.server:create_app', factory=True, host='127.0.0.1', port=port,
                workers=1, access_log=False, timeout_graceful_shutdown=1)


if __name__ == '__main__':
    main()
