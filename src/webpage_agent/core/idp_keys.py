"""中台身份提供方的签名密钥：RSA 私钥落盘，公钥以 JWKS 暴露。

三个决定都不是随便定的：

1. **私钥落盘而不是每次启动现生成。** 子平台会按 `kid` 缓存公钥，每次重启换
   一把新钥匙会让缓存里的旧 `kid` 取不到对应公钥，正在登录的用户在下次校验时
   直接 401。密钥必须跨重启稳定。

2. **私钥不进数据库。** 数据库会被整体复制去做备份或排障，密钥不该跟着走。
   这条和 `internal/auth/assertion.go` 里 BeefTV 的判断一致。

3. **`kid` 只由公钥推导。** 轮换密钥时子平台必须先发现"取不到旧 kid"才会去
   拉新 JWKS；如果 kid 是随机值，换钥匙后旧缓存仍然命中，验签会一直失败却
   看不出原因。
"""
import hashlib
import json
import logging
import os
from pathlib import Path

from joserfc.jwk import RSAKey

logger = logging.getLogger(__name__)

# 2048 位是当前下限量。再短会被部分客户端直接拒绝，再长只是拖慢握手。
_KEY_BITS = 2048


class KeyMaterialError(RuntimeError):
    """密钥不可用。启动期抛这个比运行期 500 更容易定位。"""


def _kid_of(key: RSAKey) -> str:
    """`kid` = 公钥 JWK 规范化 JSON 的 SHA-256 前 16 位。

    用公钥内容而不是随机值或时间戳，保证"同一把钥匙永远同一个 kid"。
    """
    public = key.as_dict(private=False)
    canonical = json.dumps(public, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]


def _load(path: Path) -> RSAKey:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise KeyMaterialError(f'读取签名私钥失败: {path}') from exc
    try:
        return RSAKey.import_key(raw)
    except Exception as exc:                     # noqa: BLE001
        # 常见成因：上次写到一半被 kill。不自动修复——静默重建密钥会让所有
        # 子平台缓存失效，宁可让运维看一眼再决定删不删。
        raise KeyMaterialError(
            f'签名私钥文件损坏，请确认后删除再重启: {path}') from exc


def _create(path: Path) -> RSAKey:
    """原子创建：O_EXCL 保证并发启动时只有一个进程真正落盘。

    落盘的 PEM 只写一次、体积很小，因此不做临时文件 + rename；代价是极端情况下
    可能留下截断文件，这种情况由 `_load` 的解析失败兜住并提示人工处理。
    """
    key = RSAKey.generate_key(_KEY_BITS, private=True)
    pem = key.as_pem(private=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # 另一个进程抢先建好了，用它的那份，不要覆盖。
        return _load(path)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(pem)
    except OSError as exc:
        raise KeyMaterialError(f'写入签名私钥失败: {path}') from exc
    logger.info('已生成 OIDC 签名密钥 kid=%s path=%s', _kid_of(key), path)
    return key


def load_or_create(raw_path: str) -> tuple[RSAKey, str]:
    """加载签名密钥，不存在则生成。返回 (key, kid)。

    目录会以 0700 创建：密钥所在目录不该对其他用户可读。
    """
    if not str(raw_path or '').strip():
        raise KeyMaterialError('未配置签名私钥路径（IDP_KEY_PATH）')
    path = Path(raw_path).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise KeyMaterialError(f'创建密钥目录失败: {path.parent}') from exc

    key = _load(path) if path.exists() else _create(path)
    kid = _kid_of(key)
    # 文件权限可能被复制/解压工具放宽，每次启动收紧一次，成本可忽略。
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.warning('无法收紧签名私钥权限: %s', path)
    return key, kid


def jwks(key: RSAKey, kid: str) -> dict:
    """按 RFC 7517 输出公钥集合。

    只输出公钥字段；`use`/`alg` 显式声明，避免客户端按 JWT 的 alg 去猜。
    """
    public = dict(key.as_dict(private=False))
    public.update({'use': 'sig', 'alg': 'RS256', 'kid': kid})
    return {'keys': [public]}
