import argparse
import tempfile
from pathlib import Path

DEFAULT_DIR = Path(tempfile.gettempdir()) / "ya-drone-swarm"
DEFAULT_PROVIDERS = 2

DEFAULT_CPU_QUOTA = 1024
DEFAULT_CPU_SHARES = 0.5  # core percentage
DEFAULT_MEM = 512  # MB


def arg_parser():
    parser = argparse.ArgumentParser(prog="ya-drone-swarm")
    parser.add_argument(
        "-d",
        "--dir",
        type=str,
        default=DEFAULT_DIR,
        help="override working directory",
    )

    subparsers = parser.add_subparsers(dest="cmd", help="command to run")
    parser_up = subparsers.add_parser("up", help="bring up containers")
    parser_env = subparsers.add_parser("env", help="start environment shell")
    subparsers.add_parser("ps", help="show processes")

    _args_up(parser_up)
    _args_env(parser_env)

    return parser


def _args_up(parser):
    def providers(value):
        count = int(value)
        if count <= 0:
            raise ValueError("Number of providers must be greater than 0")
        return count

    def caps(value):
        if value.lower() == "none":
            return 0, 0
        values = value.split(",")
        if len(values) != 2:
            raise ValueError(f"Invalid caps: {value}, expected 'cpu,mem'")
        return float(values[0]), int(values[1])

    default_caps = f"none"

    parser.add_argument(
        "-s",
        "--subnet",
        default="drones",
        type=str,
        help="subnet to use",
    )
    parser.add_argument(
        "-b",
        "--binary",
        default=[],
        action="append",
        nargs=2,
        metavar=("name", "path"),
        help="override binary path",
    )
    parser.add_argument(
        "-p",
        "--providers",
        default=DEFAULT_PROVIDERS,
        type=providers,
        help="number of providers",
    )
    parser.add_argument(
        "--cap-net",
        default=default_caps,
        type=caps,
        help="net resource cap [cpu_percent,mem_MB]",
    )
    parser.add_argument(
        "--cap-req",
        default=default_caps,
        type=caps,
        help="requestor resource cap [cpu_percent,mem_MB]",
    )
    parser.add_argument(
        "--cap-prov",
        default=default_caps,
        type=caps,
        help="provider resource cap [cpu_percent,mem_MB]",
    )
    parser.add_argument(
        "-c", "--central-net", action="store_true", help="use central network"
    )


def _args_env(parser):
    parser.add_argument(
        "-n",
        "--no-init",
        action="store_true",
        help="do not run requestor setup",
    )
