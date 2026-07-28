from typing import Callable,Dict,Any,List
# from ..llm.llm import LLM
from abc import abstractmethod

class Agent_Base:
    def __init__(self,llm,tools):
        self.llm = llm
        self.tools = tools
        self.history = []

    @abstractmethod
    def run(self,messages:List[Dict[str,Any]],max_steps:int = 3):
        pass

    def _execute_response(self,text:str):
        pass

    def _parse_output(self,question:str):
        pass

    def _parse_action(self, action_text: str):
        pass
            

class Base:
    def __init__(
            self,
            llm,
            max_step,
            system_prompt: str,
            tools,
            ):
        
        self.llm = llm
        self.max_step = max_step
        self.system_prompt = system_prompt
        self.tools = tools

    def run(self):
        pass

    def _execute_response(self):
        pass

    def _check_tool(self):
        pass

    def _execute_tool(self):
        pass

    def _check_message(self):
        pass