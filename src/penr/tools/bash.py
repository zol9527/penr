"""受限 Bash 工具：超时、输出截断，可选经录流代理。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass


@dataclass
class BashResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


async def run_bash(
    command: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    max_output: int = 80_000,
    env: dict[str, str] | None = None,
    http_proxy: str | None = None,
) -> BashResult:
    """执行 shell 命令；危险命令由 guard 层在调用前拦截。"""
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    if http_proxy:
        proc_env["http_proxy"] = http_proxy
        proc_env["https_proxy"] = http_proxy
        proc_env["HTTP_PROXY"] = http_proxy
        proc_env["HTTPS_PROXY"] = http_proxy

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=proc_env,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return BashResult(exit_code=-1, stdout="", stderr="command timed out", timed_out=True)
    except OSError as exc:
        return BashResult(exit_code=-1, stdout="", stderr=str(exc))

    def _clip(data: bytes) -> str:
        text = data.decode("utf-8", errors="replace")
        if len(text) > max_output:
            return text[:max_output] + f"\n...[truncated {len(text) - max_output} chars]"
        return text

    return BashResult(
        exit_code=proc.returncode or 0,
        stdout=_clip(stdout_b),
        stderr=_clip(stderr_b),
        timed_out=timed_out,
    )
