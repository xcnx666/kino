import sys
import os
from dotenv import load_dotenv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv()
from llm.llm_client import LLM
from agent.agent_client import Agent
from tools.tool import Tool
from tools import ALL_TOOLS
from logger import logger
from pathlib import Path

prompt_path = Path(__file__).parent / "prompt" / "alpha.md"
system_prompt = prompt_path.read_text(
    encoding="utf-8"
)

# 各工具的 JSON Schema 参数定义
TOOL_PARAMETERS = {
    "bash": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 Shell/Bash 命令"}
        },
        "required": ["command"],
    },
    "write": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "要写入的内容"},
        },
        "required": ["file_path", "content"],
    },
    "read": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"}
        },
        "required": ["file_path"],
    },
    "edit": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"},
            "old_text": {"type": "string", "description": "要替换的文本"},
            "new_text": {"type": "string", "description": "替换后的文本"},
        },
        "required": ["file_path", "old_text", "new_text"],
    },
}


def build_tools() -> Tool:
    """实例化所有工具类并注册到 Tool 管理器。"""
    tool = Tool()
    for ToolClass in ALL_TOOLS:
        instance = ToolClass()
        name = instance.name()
        tool.addTool(
            name=name,
            description=instance.description(),
            func=instance.func,
            parameters=TOOL_PARAMETERS.get(name, {"type": "object", "properties": {}}),
        )
    return tool

def main():
    BLUE = "\033[34m"
    RESET = "\033[0m"
    print(BLUE + r"""
        █████╗ ██╗      ██████╗ ██╗  ██╗ █████╗
        ██╔══██╗██║     ██╔══██╗██║  ██║██╔══██╗
        ███████║██║     ██████╔╝███████║███████║
        ██╔══██║██║     ██╔═══╝ ██╔══██║██╔══██║
        ██║  ██║███████╗██║     ██║  ██║██║  ██║
        ╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝
                Kino Agent
            Autonomous AI Assistant
                Status: READY 🚀
    """ + RESET)

    model = LLM()
    tool = build_tools()
    agent = Agent(
        llm = model,
        tools = tool.allTool(),
        max_llm_step = 10,
        max_tool_step = 20,
        system_prompt = system_prompt
    )
    logger.info(f'模型调用成功:({model.model})')
    while True:
        question = input('请输入问题: ')
        logger.info(f'🎙️: {question}')

        if question in ['quit','exit','退出']:
            logger.debug('Kino退出')
            break

        result = agent.run(question=question)
        if result:
            content = result.content or ""
            if "<think>" in content and "</think>" in content:
                think = content.split("<think>")[1].split("</think>")[0].strip()
                answer = content.split("</think>")[1].strip()
            else:
                answer = content.strip()
            logger.info(f"\n📖: {answer}")

if __name__ == "__main__":
    main()