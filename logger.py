import logging
import colorlog


def get_logger(
    name: str = "agent",
    level: int = logging.INFO,
) -> logging.Logger:
    """
    获取一个彩色 Logger
    """

    logger = colorlog.getLogger(name)

    # 防止重复添加 Handler
    if logger.handlers:
        return logger

    handler = colorlog.StreamHandler()

    formatter = colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    )

    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False

    return logger


# 默认导出一个 logger
logger = get_logger()