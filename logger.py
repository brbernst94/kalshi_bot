"""logger.py"""
import logging, os
from logging.handlers import RotatingFileHandler
from config import LOG_LEVEL, LOG_FILE

def setup_logging():
    os.makedirs("logs", exist_ok=True)
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    fmt   = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    ch = logging.StreamHandler()
    ch.setLevel(level); ch.setFormatter(fmt)

    fh = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=3)
    fh.setLevel(logging.DEBUG); fh.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(ch); root.addHandler(fh)
