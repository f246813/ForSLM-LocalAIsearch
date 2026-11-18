"""Tie Pipe work and its search process to the active ForSLM instance."""
import asyncio
from contextlib import suppress
import json
import os
import subprocess
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, WSMsgType

from runtime_config import RESOLVED, ROOT, read_json
from windows_job import Job


class TaskStopped(RuntimeError):
    pass


class SearchError(RuntimeError):
    pass


def model_url(settings, key):
    url = settings[key + "_url"]
    if key in settings["managed_models"]:
        host = url.removesuffix("/v1")
        return f"{host}/_forslm-task/{settings['task_id']}/v1"
    return url


async def run_task(work):
    settings = read_json(RESOLVED)
    control = settings["control"]
    task_id = uuid4().hex
    settings["task_id"] = task_id
    headers = {"X-ForSLM-Token": control["token"]}
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=3)) as client:
        async with client.ws_connect(control["url"] + "/tasks/" + task_id, headers=headers,
                                     heartbeat=15) as lease:
            running = asyncio.create_task(work(settings))
            signal = asyncio.create_task(lease.receive())
            try:
                async with asyncio.timeout(settings["task_timeout_seconds"]):
                    done, _ = await asyncio.wait((running, signal), return_when=asyncio.FIRST_COMPLETED)
                    if running in done:
                        return running.result()
                    message = signal.result()
                    if message.type == WSMsgType.TEXT and message.data == "timeout":
                        raise TimeoutError
                    if message.type == WSMsgType.TEXT and message.data == "cancel":
                        raise TaskStopped("服务正在停止")
                    raise TaskStopped("服务连接已断开")
            finally:
                for task in (running, signal):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(running, signal, return_exceptions=True)
                with suppress(ConnectionError, RuntimeError):
                    await lease.send_str("finished")


async def search(settings, query, env, status):
    loop = asyncio.get_running_loop()
    records = asyncio.Queue()

    def collect(process):
        try:
            with process.stdout:
                for line in process.stdout:
                    if line.strip():
                        loop.call_soon_threadsafe(records.put_nowait, json.loads(line))
            process.wait()
        finally:
            loop.call_soon_threadsafe(records.put_nowait, None)

    process_env = {**os.environ, **env}
    with Job() as job, (ROOT / "logs/agent-search.log").open("a", encoding="utf-8") as log:
        process = job.spawn([settings["node_path"], str(ROOT / "integration/search-bridge.mjs"), query],
                            cwd=ROOT / "upstream/agent-search", env=process_env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                            text=True, encoding="utf-8")
        reader = asyncio.create_task(asyncio.to_thread(collect, process))
        result = error = None
        try:
            async with asyncio.timeout(190):
                while (record := await records.get()) is not None:
                    match record.get("type"):
                        case "ranked":
                            await status(f"已收集 {record['candidateCount']} 条候选，BGE 选出 Top {record['selectedCount']}，正在读取网页…")
                        case "result":
                            result = record
                        case "error":
                            error = record["message"]
                await asyncio.shield(reader)
            if process.returncode or result is None:
                raise SearchError(error or "搜索失败，请查看 agent-search.log")
            return result
        except TimeoutError:
            raise SearchError("搜索超时，请稍后重试") from None
        finally:
            try:
                await asyncio.to_thread(job.terminate)
            finally:
                await asyncio.gather(reader, return_exceptions=True)
                await asyncio.to_thread(process.wait)
