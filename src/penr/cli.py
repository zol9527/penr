"""CLI 入口：penr serve | migrate | version"""

from __future__ import annotations

import argparse
import asyncio
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="penr", description="penr — AI autonomous pentest platform")
    sub = parser.add_subparsers(dest="cmd", required=False)

    serve = sub.add_parser("serve", help="启动 FastAPI 服务")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    sub.add_parser("migrate", help="执行 schema.sql 迁移")
    sub.add_parser("version", help="打印版本")

    # 兼容旧入口：无子命令时默认 serve
    args = parser.parse_args(argv)
    cmd = args.cmd or "serve"

    if cmd == "version":
        from penr import __version__

        print(__version__)
        return

    if cmd == "migrate":
        asyncio.run(_migrate())
        return

    if cmd == "serve":
        _serve(host=args.host, port=args.port)
        return

    parser.print_help()
    sys.exit(2)


async def _migrate() -> None:
    from penr.db.engine import Database

    db = Database()
    await db.connect()
    try:
        await db.migrate()
        print("migrate ok")
    finally:
        await db.close()


def _serve(host: str | None, port: int | None) -> None:
    import uvicorn

    from penr.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "penr.api.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
