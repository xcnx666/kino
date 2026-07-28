"""Web 服务启动脚本。

运行方式：
  python3 run_web.py

启动后访问 http://localhost:8000
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from logger import logger


def main():
    logger.info("=" * 50)
    logger.info("Kino - Web 服务启动")
    logger.info("=" * 50)
    logger.info("访问地址: http://localhost:8000")
    logger.info("API 文档: http://localhost:8000/docs")
    logger.info("按 Ctrl+C 停止服务")
    logger.info("=" * 50)

    uvicorn.run(
        "web.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
