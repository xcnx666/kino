"""端到端演示脚本：验证视频生成闭环。

运行方式：
  python3 run_demo.py

演示模式不需要 LLM / Agnes API key，用 ffmpeg 生成占位素材验证合成流程。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from media_tools import build_registry_without_agnes
from pipeline.orchestrator import VideoOrchestrator
from logger import logger


async def main():
    logger.info("启动视频生成平台 - 演示模式")
    logger.info("=" * 50)

    # 1. 构建工具注册中心（不含 Agnes，不需要 API key）
    registry = build_registry_without_agnes()
    logger.info(f"已注册工具: {registry.names()}")

    # 2. 创建编排器
    orchestrator = VideoOrchestrator(registry)

    # 3. 运行演示
    project = await orchestrator.demo_run(project_name="demo_video")

    # 4. 验证结果
    logger.info("=" * 50)
    if project.final_path and os.path.exists(project.final_path):
        size = os.path.getsize(project.final_path)
        logger.info(f"✅ 闭环测试成功！")
        logger.info(f"   输出文件: {project.final_path}")
        logger.info(f"   文件大小: {size:,} bytes ({size/1024/1024:.2f} MB)")
        logger.info(f"   分镜数量: {len(project.shots)}")
    else:
        logger.error(f"❌ 闭环测试失败，未生成输出文件")

    return project


if __name__ == "__main__":
    asyncio.run(main())
