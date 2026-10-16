"""启动 FastAPI + SSE 服务。

    python -m scripts.serve_api                    # 默认 127.0.0.1:8000
    python -m scripts.serve_api --port 9000        # 换端口
    python -m scripts.serve_api --reload           # 开发模式，改代码自动重启

启动后浏览器打开 http://127.0.0.1:8000/ 即可使用聊天界面。
"""
import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description='启动网页研究报告 API 服务')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--reload', action='store_true', help='开发模式：改代码自动重启')
    args = parser.parse_args()
    uvicorn.run('app.api:app', host=args.host, port=args.port, reload=args.reload)


if __name__ == '__main__':
    main()
