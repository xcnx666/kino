from typing import List,Dict,Any

class ToolStatus:
    content:str
    ToolStatus:bool


class Base:
    def name(self) -> str:
        pass

    def description(self) -> str:
        pass

    def func(self):
        pass

    def execute(self,name:str):
        pass