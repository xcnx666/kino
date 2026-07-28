from agent.base import Agent_Base
from llm.llm import LLM
from typing import List,Dict,Any
from prompt.plan_prompt import PLAN_PROMPT
from prompt.react_prompt import REACT_PROMPT_TEMPLATE
from tools.execute import ExecuteTool
from pathlib import Path
import os
from logger import logger
import re
import ast


class Agent(Agent_Base):
    def __init__(self, llm:LLM, tools:ExecuteTool):
        super().__init__(llm, tools)
        self.llm = llm
        self.history = []
        self.tool = tools

    def run(self,max_tep:int = 3,question:str = None):

        question,task = self._execute_response(question)
        logger.info(f'task:{task}')

        index = 0
        tep = 0

        while index < len(task):
            logger.info(f'正在执行第{index+1}步')
            plan_step = task[index]

            react_prompt = REACT_PROMPT_TEMPLATE.substitute(
                    question = question,
                    plan_steps = task,
                    plan_step = plan_step,
                    history = self.history,
                    tools = self.tool,
                    file_path = Path(__file__).resolve().parent
            )

            messages = [
                {'role':'user','content':react_prompt}
            ]

            while tep < max_tep:

                react_response = self.llm.chat(
                    messages = messages
                )

                messages.append(
                    {'role':'assistant','content':react_response}
                )

                Thought, Action = self._parse_output(react_response)
                
                if Thought:
                    logger.info(f'🧠:{Thought}')

                if not Action:
                    logger.warning('未能解析出有效的Action，流程终止。')
                    break
                
                tool_name, tool_input = self._parse_action(Action)

                if tool_name == "Finish":
                    print(f"📖: {tool_input}")
                    return tool_input

                if not tool_name or not tool_input:
                    continue

                logger.info(f'🔧:正在调用({tool_name})')
                func = self.tool.getTool(tool_name)
                result = func(tool_input)

                if not result:
                    observation = logger.warning(f'没有找到{tool_name}的工具')

                else:
                    observation = logger.info(f'工具{tool_name}调用成功：{result}')

                logger.info(f'👀:{observation}')

                logger.info(f'{task[index]}---执行成功')

                self.history.append(f"Action: {Action}")
                self.history.append(f"Observation: {observation}")
                tep +=1

            index += 1

        return None

    def _execute_response(self,question:str):
        
        prompt = PLAN_PROMPT.format(
            question = question
        )

        messages = [
            {'role':'system','content':prompt},
            {'role':'user','content':question}
        ]

        response = self.llm.chat(
            messages = messages
        )

        if not response:
            logger.info('模型生成失败')

        if '```python' and  '```' in response:

            task_str = response.split('```python')[1].split('```')[0].strip()
            task = ast.literal_eval(task_str)
        else:
            task = response

        return question,task
    
    def _parse_output(self,response:str):

        Thought_match = re.search(r'Thought:\s*(.*?)(?=\nAction:|$)',response,re.DOTALL)
        Action_match = re.search(r"Action:\s*(.*?)$", response, re.DOTALL)

        Thought = Thought_match.group(1).strip() if Thought_match else None
        Action = Action_match.group(1).strip() if Action_match else None

        return Thought,Action

    def _parse_action(self, action_content: str):

        action = re.match(r"(\w+)\[(.*)\]", action_content, re.DOTALL)
        if action:
            return action.group(1), action.group(2)

        return None, None