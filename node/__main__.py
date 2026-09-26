"""node/__main__.py — run with:  python -m node"""
import uvicorn

from node import config

if __name__ == "__main__":
    uvicorn.run("node.app:app", host=config.HOST, port=config.PORT, log_level="info")
