from typing import List,Dict,Any,Callable
import sys
import os
from tools import *
import logging
import colorlog

handler = colorlog.StreamHandler()
handler.setFormatter(
    colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s | %(levelname)s | %(message)s",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    )
)
logger = colorlog.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(handler)


class ExecuteTool:
    def __init__(self):
        self.tools:Dict[str,Dict[str,Any]] = {}

    def addTool(self,name:str,description:str,func:callable):
        if name in self.tools:
            logger.warning(f'🔧{name}已经存在，请勿重复添加')

        self.tools[name] = {'description': description, 'func': func}
        logger.info(f'🔧{name}添加成功')

    def getTool(self,name:str):

        return self.tools.get(name,{}).get('func')

    def allTool(self):

        return "\n".join([
            f"- {name}: {info['description']}" 
            for name, info in self.tools.items()
        ])

