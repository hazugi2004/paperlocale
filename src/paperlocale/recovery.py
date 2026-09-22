"""等待式恢复：不把异常变成成功，也不无限调用有费用的模型。

每次操作仍使用原有原子断点。瞬时网络故障仅自动重试一次；其余错误写入
waiting.json，保持进程存活，直到外部修正问题后执行 ``resume-waiting``。
用户取消和操作系统终止不属于可吞掉的异常。电源故障不可能由本进程保证恢复。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError

T = TypeVar('T')


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _message(error: Exception) -> str:
    # Provider 的错误可能包含 URL 参数或 Authorization；等待文件只保留诊断，
    # 不能成为凭据日志。内置密钥格式及当前明确的 API 环境变量均脱敏。
    value = str(error)
    for name, secret in os.environ.items():
        if ('KEY' in name or 'TOKEN' in name) and len(secret) >= 8:
            value = value.replace(secret, '[redacted]')
    value = re.sub(r'sk-[^\s\"\'<>]+', '[redacted]', value)
    value = re.sub(r'(?i)(bearer\s+)\S+', r'\1[redacted]', value)
    return value[-3000:]


def _transient(error: Exception) -> bool:
    # 不对普通 ValueError/RuntimeError 猜测性重试：它们可能是内容校验、
    # 磁盘空间或身份校验失败。HTTP 401/403 等永久问题直接等待外部恢复。
    from .providers.qwen_mt import QwenRequestError
    if isinstance(error, QwenRequestError):
        return False
    seen = set()
    while error.__cause__ is not None and id(error) not in seen:
        seen.add(id(error))
        error = error.__cause__
    if isinstance(error, HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504}
    return isinstance(error, (TimeoutError, ConnectionError, URLError))


def resume_waiting(root: Path) -> None:
    """明确请求活跃等待进程重试；信号绑定当次错误，旧信号不能触发新错误。"""
    root = root.expanduser().resolve()
    status = json.loads((root / 'waiting.json').read_text(encoding='utf-8'))
    if status.get('state') != 'waiting':
        raise ValueError('当前任务没有处于等待状态')
    _save(root / 'resume.request', {'error_id': status['error_id']})


def run_waiting(operation: Callable[[], T], root: Path, *,
                retry_delay: float = 30, poll_seconds: float = 2,
                sleep: Callable[[float], None] = time.sleep) -> T:
    """返回真实操作结果。未完成时只等待，不返回伪成功或含缺口候选。

    sleep 参数便于测试恢复状态机，不在测试中等待真实网络。文件恢复信号是本地
    用户明确操作，不是后台反复翻译；轮询只检查小文件，不调用模型或版面引擎。
    """
    if retry_delay < 0 or poll_seconds <= 0:
        raise ValueError('等待间隔必须为非负重试间隔和正轮询间隔')
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    retried = False
    while True:
        try:
            result = operation()
        except Exception as error:
            identity = str(time.time_ns())
            automatic = _transient(error) and not retried
            status = {'state': 'retrying' if automatic else 'waiting',
                      'error_id': identity, 'error_type': type(error).__name__,
                      'message': _message(error), 'pid': os.getpid(),
                      'automatic_retry_used': retried}
            _save(root / 'waiting.json', status)
            if automatic:
                retried = True
                print(f'PaperLocale：已保存断点，{retry_delay:g} 秒后重试一次。', flush=True)
                sleep(retry_delay)
                continue
            print(f'PaperLocale：保持等待，原因见 {root / "waiting.json"}。修正后执行 '
                  f'paperlocale resume-waiting --run-dir "{root}"。', flush=True)
            while True:
                request = root / 'resume.request'
                if request.exists():
                    try:
                        signal = json.loads(request.read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        signal = {}
                    if signal.get('error_id') == identity:
                        request.unlink()
                        break
                sleep(poll_seconds)
            # 明确恢复信号仅允许再执行一次；同一故障仍存在就继续等待。
            retried = True
        else:
            _save(root / 'waiting.json', {'state': 'completed', 'pid': os.getpid()})
            return result
