from .base import Base


class Edit(Base):
    def name(self) -> str:
        return "edit"

    def description(self) -> str:
        return """
Description:
编辑本地文本文件，将指定内容替换为新的内容。

Args:
- file_path (str): 文件路径。
- old_text (str): 要替换的文本。
- new_text (str): 替换后的文本。

Returns:
- str: 编辑结果。

Example:
{
    "file_path": "./main.py",
    "old_text": "print('hello')",
    "new_text": "print('Hello World')"
}
"""

    def func(self, file_path: str, old_text: str, new_text: str) -> str:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            if old_text not in content:
                return "Error: old_text not found."

            content = content.replace(old_text, new_text, 1)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            return "Successfully edited file."

        except Exception as e:
            return f"Error: {e}"