from .base import Base

class Read(Base):
    def name(self) -> str:

        return "read"

    def description(self) -> str:

        return """

        读取本地文本文件内容。

        Args:

            file_path (str): 文件路径。

        Example:

            {"file_path": "./main.py"}

        """

    def func(self, file_path: str) -> str:

        try:

            with open(file_path, "r", encoding="utf-8") as f:

                return f.read()

        except FileNotFoundError:

            return f"Error: File not found: {file_path}"

        except IsADirectoryError:

            return f"Error: {file_path} is a directory."

        except UnicodeDecodeError:

            return f"Error: {file_path} is not a UTF-8 text file."

        except Exception as e:

            return f"Error: {e}"