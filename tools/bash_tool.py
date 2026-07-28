import platform
import subprocess
from .base import Base

class Bash(Base):

    def name(self) -> str:

        return "bash"

    def description(self) -> str:

        return """

Description:

执行本地终端命令。

Args:

- command (str): 要执行的 Shell/Bash 命令。

Returns:

- str: 命令执行结果（stdout 或 stderr）。

Example:

{"command":"ls -la"}

"""

    
    def func(self, command: str) -> str:

        system = platform.system()

        # Windows 使用 PowerShell，其它系统使用 Bash

        if system == "Windows":

            shell = ["powershell", "-Command", command]

        else:

            shell = ["/bin/bash", "-c", command]

        try:

            result = subprocess.run(

                shell,

                capture_output=True,

                text=True,

                encoding="utf-8",

            )

            if result.returncode == 0:

                return result.stdout.strip()

            return result.stderr.strip()

        except Exception as e:

            return f"Error: {e}"