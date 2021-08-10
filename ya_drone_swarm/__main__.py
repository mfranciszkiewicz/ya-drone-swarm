import asyncio
import copy
import json
import logging
import os
import random
import re
import shutil
import string
import subprocess
import sys

from concurrent.futures.thread import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import aiodocker
import coloredlogs
import docker

from docker.errors import ImageNotFound
from ya_drone_swarm.cli import arg_parser, DEFAULT_CPU_QUOTA

IMAGE_TAG = "ya-drone-node:0.1"

logger: Optional[logging.Logger] = None


def container_params(mangle_fn, **overrides):
    params = {
        "image": IMAGE_TAG,
        "privileged": True,
        "auto_remove": True,
        "volumes": dict(),
        "environment": dict(),
    }

    if mangle_fn:
        mangle_fn(params)

    params.update(overrides)
    return params


def container_caps(caps):
    if caps[0] == 0 and caps[1] == 0:
        return dict()
    return {
        "cpu_quota": DEFAULT_CPU_QUOTA,
        "cpu_shares": int(caps[0] * DEFAULT_CPU_QUOTA),
        "mem_limit": f"{caps[1]}m",
    }


class Context:
    def __init__(self, args, **overrides):
        self.dir = None
        self.subnet = None
        self.binary = None
        self.central_net = None

        self.providers = 1
        self.ports = {"net": 7463, "requestor": 7464}
        port = self.ports["requestor"] + 2
        self.net_ip = "0.0.0.0"

        for key, value in vars(args).items():
            setattr(self, key, value)
        for key, value in overrides.items():
            setattr(self, key, value)

        self.cap_net = container_caps(self.cap_net)  # noqa
        self.cap_req = container_caps(self.cap_req)  # noqa
        self.cap_prov = container_caps(self.cap_prov)  # noqa

        self.dirs = {
            "work": args.dir,
            "bin": args.dir / "bin",
            "requestor": args.dir / "requestor",
            "requestor/yagna": args.dir / "requestor" / "yagna",
            "net": args.dir / "net",
            "net/yagna": args.dir / "net" / "yagna",
        }

        for i in range(self.providers):
            provider = f"provider{i}"

            self.dirs[provider] = args.dir / provider
            self.dirs[f"{provider}/yagna"] = self.dirs[provider] / "yagna"

            self.ports[provider] = port
            port += 2

    def config_net(self, **overrides):
        def mangle_fn(params):
            port = self.ports["net"]
            params.update(
                {
                    "command": f"bash -c 'mkdir -p /root/.local/share/yagna && "
                    f"RUST_LOG=trace ya-sb-router -l tcp://0.0.0.0:{port} 2>&1 "
                    "| tee -a /root/.local/share/yagna/provider.log'",
                    "ports": {
                        f"{port}/tcp": f"{port}",
                    },
                    "volumes": [
                        f"{str(self.dirs['bin'])}:/yagna/bin",
                        f"{str(self.dirs['net/yagna'])}:/root/.local/share/yagna",
                    ],
                }
            )

        return container_params(mangle_fn, **self.cap_net, **overrides)

    def config_req(self, **overrides):
        def mangle_fn(params):
            port = self.ports["requestor"]
            params.update(self._config_env("requestor"))
            params.update(
                {
                    "command": "bash -c 'yagna service run'",
                    "ports": {
                        f"{port}/tcp": f"{port}",
                        f"{port + 1}/tcp": f"{port + 1}",
                    },
                }
            )

        return container_params(mangle_fn, **self.cap_req, **overrides)

    def config_prov(self, idx: int, **overrides):
        def mangle_fn(params):
            params.update(self._config_env(f"provider{idx}"))
            params.update(
                {
                    "command": (
                        f"bash -c 'echo 'fs.inotify.max_user_watches=32000' | tee -a /etc/sysctl.conf && "
                        "sysctl -p > /dev/null && "
                        "ya-provider profile update default --cpu-threads 1 --mem-gib 0.128 --storage-gib 0.5 && "
                        f"golemsp run --payment-network rinkeby --subnet {self.subnet} 2>&1 "
                        "| tee /root/.local/share/yagna/provider.log'"
                    ),
                }
            )

        return container_params(mangle_fn, **self.cap_prov, **overrides)

    def _config_env(self, name):
        port = self.ports[name]
        gsb, rest = port, port + 1

        params = {
            "ports": {f"{gsb}/tcp": f"{gsb}", f"{rest}/tcp": f"{rest}"},
            "volumes": [
                f"{str(self.dirs['bin'])}:/yagna/bin",
                f"{str(self.dirs[name] / 'yagna')}:/root/.local/share/yagna",
            ],
            "environment": {
                "GSB_URL": f"tcp://0.0.0.0:{gsb}",
                "YAGNA_API_URL": f"http://0.0.0.0:{rest}",
            },
        }

        if not self.central_net:
            params["environment"][
                "CENTRAL_NET_HOST"
            ] = f"{self.net_ip}:{self.ports['net']}"

        return params


class Runner:
    def __init__(self, ctx: Context):
        self.__containers = []
        self.__ctx = ctx
        self.__task: Optional[asyncio.Task] = None

    async def wait(self):
        if self.__task:
            await self.__task

    def remove_all(self):
        containers = self.__containers
        self.__containers = []

        if len(containers) == 0:
            return

        executor = ThreadPoolExecutor(max_workers=len(containers))
        [executor.submit(lambda: docker_remove(n)) for n in containers]

    async def __aenter__(self):
        self.__setup_workdir()
        self.__link_binaries()

        if not self.__ctx.central_net:
            name = "net"
            self.__containers.append(name)
            docker_run_detached(name, self.__ctx.config_net())
            await docker_log(name, ".*Starting [\\d]+ workers")
            self.__ctx.net_ip = docker_ip(name)
            logger.info(f"net address : {self.__ctx.net_ip}:{self.__ctx.ports['net']}")

        logger.info(f"starting containers")

        name = "requestor"
        self.__containers.append(name)
        futures = [docker_run(name, self.__ctx.config_req())]

        for i in range(self.__ctx.providers):
            name = f"provider{i}"
            self.__containers.append(name)
            futures.append(docker_run(name, self.__ctx.config_prov(i)))

        self.__task = asyncio.create_task(asyncio.wait(futures))
        return self.__task

    async def __aexit__(self, exc_type, exc, tb):
        await self.wait()
        self.remove_all()

    def __setup_workdir(self):
        ctx = self.__ctx

        for directory in ctx.dirs.values():
            os.makedirs(directory, exist_ok=True)

        with open(ctx.dirs["work"] / ".env", "w") as dotenv:
            dotenv.write(f"SUBNET={ctx.subnet}\n")
            dotenv.write(f"GSB_URL=tcp://0.0.0.0:{ctx.ports['requestor']}\n")
            dotenv.write(f"YAGNA_API_URL=http://0.0.0.0:{ctx.ports['requestor'] + 1}\n")

    def __link_binaries(self):
        for (name, path) in self.__ctx.binary:
            binary = Path(self.__ctx.dirs["bin"]) / name
            if os.path.exists(binary):
                os.unlink(binary)
            os.symlink(path, binary)


async def exec_wait(cmd: str, ignore_errors: bool = False, **kwargs):
    proc = await asyncio.create_subprocess_shell(cmd, **kwargs)
    result = await proc.wait()
    if result == 1 and not ignore_errors:
        raise RuntimeError(f"{cmd} finished with exit code {result}")


def docker_build():
    from os.path import dirname, abspath

    def extract_log(b) -> str:
        d = json.loads(b)
        if d and "stream" in d:
            return d["stream"].rstrip()
        else:
            return ""

    parent = Path(dirname(abspath(__file__))) / "docker"
    client = docker.APIClient()

    try:
        logger.info(f"building docker image: {IMAGE_TAG}")
        for item in client.build(path=str(parent), rm=True, tag=IMAGE_TAG):
            logger.debug(f"build: {extract_log(item)}")
    finally:
        client.close()


async def docker_run(name: str, config: dict):
    from docker.models.containers import _create_container_args  # noqa

    client = docker.APIClient()

    try:
        config = copy.deepcopy(config)
        config["version"] = client.api_version

        create_kwargs = _create_container_args(config)
        create_config = client.create_container_config(**create_kwargs)
    finally:
        client.close()

    client = aiodocker.Docker()

    try:
        container = await client.containers.create_or_replace(name, create_config)
        await container.start()

        log = await docker_log(name, ".*using default identity:.*")
        if log:
            matches = re.match(".*using default identity: ([^\s]+).*", log)
            if matches:
                groups = matches.groups()
                if groups and groups[0]:
                    logger.info(f"{name:<12}: {groups[0]}")

        await container.wait()
        logger.info(f"{name:<12} exited")
    finally:
        await client.close()


def docker_run_detached(name: str, config: dict) -> str:
    logger.info(f"starting {name} (detached)")

    client = docker.DockerClient()
    try:
        return client.containers.run(name=name, detach=True, **config)
    finally:
        client.close()


def docker_remove(name: str):
    logger.info(f"removing {name}")

    client = None
    try:
        client = docker.APIClient()
        try:
            client.kill(name)
        except:  # noqa
            pass
        client.remove_container(name, force=True)
    except:  # noqa
        pass
    finally:
        if client:
            client.close()


def docker_ip(name: str) -> str:
    client = docker.DockerClient()
    container = client.containers.get(name)
    return container.attrs["NetworkSettings"]["IPAddress"]


async def docker_log(name: str, regex: str) -> Optional[str]:
    client = aiodocker.Docker()

    try:
        containers = aiodocker.docker.DockerContainers(client)
        container = await containers.get(name)
        if not container:
            raise RuntimeError(f"Container not found: {name}")

        async for line in container.log(
            stdout=True, stderr=True, logs=True, follow=True
        ):
            if line and re.match(regex, line):
                return line

    except aiodocker.DockerError as err:
        if err.status != 404:
            logger.error(f"docker error: {err}")
    finally:
        await client.close()
    return None


def cmd_up(args):
    ctx = Context(args)

    logger.info(f"workdir     : {ctx.dirs['work']}")
    logger.info(f"providers   : {ctx.providers}")
    logger.info(f"network     : {'central' if ctx.central_net else 'local'}")

    logger.info(f"cap req     : {ctx.cap_req}")
    logger.info(f"cap prov    : {ctx.cap_prov}")
    if not ctx.central_net:
        logger.info(f"cap net     : {ctx.cap_net}")

    if ctx.binary:
        binaries = {name: path for (name, path) in ctx.binary}
        logger.info(f"binaries    : {json.dumps(binaries, indent=4, sort_keys=True)}")

    client = docker.DockerClient()

    try:
        client.images.get(IMAGE_TAG)
    except ImageNotFound:
        docker_build()
    finally:
        client.close()

    runner = Runner(ctx)

    async def run():
        async with runner as t:
            await t

    loop = asyncio.get_event_loop()
    task = loop.create_task(run())

    try:
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        logger.warning("interrupted")
    finally:
        runner.remove_all()
        loop.run_until_complete(task)
        logger.info("shutting down")


def cmd_env(args):
    env = copy.deepcopy(dict(os.environ.items()))
    opts = {
        "env": env,
        "cwd": args.dir,
    }

    for prerequisite in ["yagna"]:
        if not shutil.which(prerequisite):
            logger.error(f"missing prerequisite: {prerequisite}")
            sys.exit(1)

    def read_env(f):
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, val = line.split("=", 1)
            env[key] = val

    logger.info(f"reading .env from {args.dir}")

    try:
        with open(args.dir / ".env", "r") as dotenv:
            read_env(dotenv)
    except BaseException as exc:  # noqa
        logger.error(f"failed to read .env: {exc}")
        return

    if args.no_init:
        logger.info("skipping requestor initialization")
    else:
        name = "".join(random.choices(string.ascii_uppercase + string.digits, k=24))

        if "YAGNA_APPKEY" not in env:
            try:
                output = subprocess.check_output(
                    ["yagna", "app-key", "create", name], **opts
                )
                app_key = output.decode("utf-8").strip().split("\n")[-1]
                if app_key:
                    logger.info(f"app key: {app_key}")
                else:
                    raise RuntimeError("unknown")
            except BaseException as exc:  # noqa
                logger.error(f"failed to create application key: {exc}")
                return
            else:
                env["YAGNA_APPKEY"] = app_key

        subprocess.check_call(["yagna", "payment", "fund"], **opts)
        subprocess.check_call(["yagna", "payment", "init", "--sender"], **opts)

    logger.info("entering shell")

    del opts["cwd"]
    subprocess.call([os.environ["SHELL"]], **opts)


def cmd_ps(args):
    subprocess.call(["watch", "docker", "ps"], cwd=args.dir)


def main():
    coloredlogs.install(
        fmt="%(asctime)s.%(msecs)03d %(name)s[%(process)d] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        level="INFO",
        milliseconds=True,
    )

    global logger
    logger = logging.getLogger("drones")

    try:
        args = arg_parser().parse_args()
    except:  # noqa
        return

    if args.cmd == "up":
        cmd_up(args)
    elif args.cmd == "env":
        cmd_env(args)
    elif args.cmd == "ps":
        cmd_ps(args)
    else:
        raise RuntimeError(f"invalid command: {args.cmd}")


if __name__ == "__main__":
    main()
