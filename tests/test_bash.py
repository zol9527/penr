import pytest

from penr.tools.bash import run_bash


@pytest.mark.asyncio
async def test_run_bash_echo():
    result = await run_bash("echo hello-antifox")
    assert result.exit_code == 0
    assert "hello-antifox" in result.stdout


@pytest.mark.asyncio
async def test_run_bash_timeout():
    result = await run_bash("sleep 5", timeout=0.2)
    assert result.timed_out is True
