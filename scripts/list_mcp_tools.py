import asyncio
import json
from app.config import settings  # 加载 .env
from app.mcp_adapter import list_server_tools

if __name__ == '__main__':
    print(json.dumps(asyncio.run(list_server_tools()), ensure_ascii=False, indent=2))
