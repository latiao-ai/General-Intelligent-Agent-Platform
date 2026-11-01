"""探测本机在局域网里的可用 IPv4 地址。

不能只用「UDP 连 192.0.2.1 再看 socket 名字」这一招：本机开了代理
（ClashX 之类的 TUN），路由表会把它导到 `198.18.0.0/15` 这个基准测试保留
网段，取回来的是 `198.18.0.1` —— 用户照着这个地址访问不到，日志里也会
把 `AUTH_ALLOWED_ORIGINS` 指到错的地方。

做法：先按主机名解析出所有 IPv4，再兜底用 UDP trick，统一过滤：
丢弃回环、链路本地、`198.18/15`，优先返回 RFC1918 私有地址。
"""
import ipaddress
import socket

# RFC1918 私有网段：局域网里真正能访问到的地址
_PRIVATE = (ipaddress.ip_network('10.0.0.0/8'),
            ipaddress.ip_network('172.16.0.0/12'),
            ipaddress.ip_network('192.168.0.0/16'))
# 代理软件 TUN 的保留网段（RFC 2544 基准测试），不是真实局域网地址
_BENCHMARK = ipaddress.ip_network('198.18.0.0/15')


def _addresses():
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('192.0.2.1', 9))
            found.append(sock.getsockname()[0])
    except OSError:
        pass
    seen = []
    for addr in found:
        if addr not in seen:
            seen.append(addr)
    return seen


def _parsed(addr):
    try:
        return ipaddress.ip_address(addr)
    except ValueError:
        return None


def lan_address():
    """返回可用于局域网访问的 IPv4 字符串；探测不到返回 None。"""
    candidates = [ip for ip in map(_parsed, _addresses()) if ip is not None]
    candidates = [ip for ip in candidates
                  if ip.version == 4 and not ip.is_loopback and not ip.is_link_local
                  and not ip.is_multicast and ip not in _BENCHMARK]
    for ip in candidates:
        if any(ip in net for net in _PRIVATE):
            return str(ip)
    for ip in candidates:
        if ip.is_private:
            return str(ip)
    return None
