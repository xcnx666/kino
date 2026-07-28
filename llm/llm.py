from .base import LLM_BASE
from dotenv import load_dotenv
from openai import OpenAI
from typing import List,Dict,Any
# from prompt.system_prompt import PLANNER_PROMPT_TEMPLATE
import os
import logging 

load_dotenv()
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

class LLM(LLM_BASE):
    def __init__(self, api_key = None, model = None, base_url = None):
        # 先从环境变量获取配置
        self.api_key = api_key or os.getenv('api_key')
        self.base_url = base_url or os.getenv('base_url')
        self.model = model or os.getenv('model')

        if not all([self.api_key, self.base_url]):
            logger.error("api_key 或 base_url 不能为空")
            raise ValueError("api_key 或 base_url 不能为空")

        # 再调用父类初始化
        super().__init__(self.api_key, self.model, self.base_url)

    def chat(self,messages: List[Dict[str, str]] ,temperature:int = 0.5) -> str :

        logger.info('正在调用模型......')
        try:
            # logger.info(f'模型调用成功({self.model})')
            response = self.client.chat.completions.create(
                messages = messages,
                temperature = temperature,
                model = self.model or os.getenv('model'),
                stream=True
            )

            contents = []
            for i in response:
                content = i.choices[0].delta.content
                if content:
                    # print(content,end = '',flush=True)
                    contents.append(content)
            return ''.join(contents)
        
        except Exception as e:
            logger.error(f'模型调用失败{e}')
            raise

