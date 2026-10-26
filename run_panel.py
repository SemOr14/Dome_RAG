"""Start the integrated local panel; never connects to equipment automatically."""

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PANEL_PORT", "8080"))
    os.environ.setdefault("DATA_API_URL", f"http://127.0.0.1:{port}")
    print(
        f"Панель: http://127.0.0.1:{port}/ui/ ; подключение к Куполу выключено до нажатия кнопки"
    )
    uvicorn.run(
        "integration.server:create_app",
        factory=True,
        host="127.0.0.1",
        port=port,
        workers=1,
        access_log=False,
    )
