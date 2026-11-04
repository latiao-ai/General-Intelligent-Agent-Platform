"""测试前置检查：确认 PG_TEST_DSN 真的连得上。

由 scripts/run_tests.sh 在跑用例之前调用，连不上就以非零码退出。

为什么单独立一个脚本而不是在 shell 里内联一段 python：内联要处理引号与
heredoc 嵌套，容易在某个 shell 上悄悄失效；独立文件让"用哪条连接串、用哪个
驱动"这件事和用例本身完全一致，出问题也只需看这一个地方。

退出码：0 连得上；1 连不上（原因打到 stderr）。
"""
import sys

USAGE = '用法：python scripts/check_test_db.py <PG_TEST_DSN>'


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].strip():
        print(USAGE, file=sys.stderr)
        return 1
    dsn = argv[1].strip()
    try:
        import psycopg
    except ImportError as exc:
        # 驱动没装和连不上是同一类问题：这条命令跑不了，不该假装通过
        print(f'无法导入 psycopg：{exc}', file=sys.stderr)
        return 1
    try:
        # connect_timeout：端口没开时不要挂在那里等默认的几十秒
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute('SELECT 1')
    except Exception as exc:                       # noqa: BLE001 —— 任何失败都是"连不上"
        print(f'{type(exc).__name__}: {exc}'.strip(), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
