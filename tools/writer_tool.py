from .base import Base

class Write(Base):

    def name(self) -> str:

        return "write"

    def description(self) -> str:

        return """

        Description:

        写入内容到本地文本文件。

        Args:

            - file_path (str): 文件路径。

            - content (str): 要写入的内容。

        Returns:

            - str: 写入结果。

            Example:

            {"file_path":"./output.txt","content":"Hello World"}

        """

    def func(self, file_path: str, content: str) -> str:

        try:

            with open(file_path, "w", encoding="utf-8") as f:

                f.write(content)

            return f"Successfully wrote to {file_path}"

        except Exception as e:

            return f"Error: {e}"