from typing import List,Dict,Callable
from .base import Base
from logger import logger
import json

class Tool(Base):
    def __init__(
            self,
            name:str=None,
            description:str=None,
            func:callable=None,
            parameters: dict=None
            ):
        
        self.name = name
        self.description = description
        self.func = func
        self.parameters = parameters
        self.tools = []
        self.funcs = {}
        
    def addTool(self,name:str,description:str,func,parameters):

        if not all([name, description, func]):
            logger.warning('name,description,func不能缺')

        for tool in self.tools:
            if tool["function"]["name"] == name:
                logger.warning(f"🔧：{name} 已存在")
                return

        self.tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters
                }
            })
        
        self.funcs[name] = (func)

        logger.info(f'🔧：{name}添加成功')
        
        return ''
    
    def getTool(self,name):

        if name not in self.funcs:
            logger.warning(f'🔧：{name}不存在')
            return None
        
        return self.funcs[name]
    
    def allTool(self):
        return self.tools
    
    def execute(self,name,arguments):

        tool = self.getTool(name)
        if tool is None:
            return {"error": f"工具 {name} 不存在"}
        # arguments 可能是 dict（Agent 已解析）或 JSON 字符串，统一处理
        if isinstance(arguments, str):
            args = json.loads(arguments)
        else:
            args = arguments or {}
        tool_response = tool(**args)

        return tool_response