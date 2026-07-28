from openai import OpenAI
from typing import List,Dict,Any
from abc import  abstractmethod
from pydantic import BaseModel
from logger import logger

# class LLM_BASE:
#     def __init__(self,api_key:str = None,model:str = None,base_url:str = None):
#         self.api_key = api_key
#         self.model = model
#         self.base_url = base_url
#         if not all([self.api_key,self.base_url]):
#             logger.error("api_key 或 base_url 不能为空")
#             raise ValueError("api_key 或 base_url 不能为空")

#         self.client = OpenAI(
#             api_key = self.api_key,
#             base_url = self.base_url
#         )

#     @abstractmethod
#     def chat(self,messages:List[Dict[str,Any]]) -> str :
#         pass

class LLM_BASE:
    def __init__(self,api_key:str,base_url:str,model:str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def _requests_model_api(self,messages:List[Dict[str,str]],tools: List[Any]):

        pass

    def _Tool_convert(self,tools:List[Any]):

        pass

    def _Messages_convert(self,messages:List[Dict[str,str]]):

        pass

    def chat(self,messages:List[Dict[str,str]],tools:List[Any]) -> str:

        pass

    def to_openai_schema(self) -> dict[str, Any]:
        pass