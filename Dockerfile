FROM python:3.11-slim

# 安装 ffmpeg（视频合成必需）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 创建必要的目录
RUN mkdir -p uploads output config sessions

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["python3", "run_web.py"]
