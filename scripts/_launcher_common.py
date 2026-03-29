#!/usr/bin/env python3
"""Shared launcher helpers for the standalone Hyperliquid strategy suite."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
RUNS = ROOT / "runs"
LOCAL_VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def default_python_executable() -> str:
    """Prefer the suite-local virtualenv when it exists."""
    if LOCAL_VENV_PYTHON.exists():
        return str(LOCAL_VENV_PYTHON)
    return sys.executable


def launch_environment(python_executable: str) -> Dict[str, str]:
    """Build a child-process environment with a known CA bundle when available."""
    env = os.environ.copy()
    try:
        cert_path = subprocess.check_output(
            [python_executable, "-c", "import certifi; print(certifi.where())"],
            text=True,
        ).strip()
    except Exception:
        cert_path = ""
    if cert_path:
        env.setdefault("SSL_CERT_FILE", cert_path)
        env.setdefault("REQUESTS_CA_BUNDLE", cert_path)
        env.setdefault("CURL_CA_BUNDLE", cert_path)
    return env


def kebab(name: str) -> str:
    return name.replace("_", "-")


def config_to_cli_args(config: Dict[str, Any], *, skip: Iterable[str] = ()) -> List[str]:
    args: List[str] = []
    skip_set = set(skip)
    for key, value in config.items():
        if key in skip_set or value is None:
            continue
        flag = f"--{kebab(key)}"
        if isinstance(value, bool):
            args.append(flag if value else f"--no-{kebab(key)}")
            continue
        args.extend([flag, str(value)])
    return args


def timestamped_run_name(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}"


def make_run_paths(
    *,
    strategy_slug: str,
    market_slug: str,
    run_name: str,
    include_fills: bool = False,
    include_markouts: bool = False,
) -> Dict[str, Path]:
    run_dir = RUNS / strategy_slug / market_slug / run_name
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, Path] = {
        "run_dir": run_dir,
        "reports_dir": reports_dir,
        "events_jsonl": run_dir / "events.jsonl",
        "samples_jsonl": run_dir / "samples.jsonl",
        "trades_csv": run_dir / "trades.csv",
    }
    if include_fills:
        paths["fills_csv"] = run_dir / "fills.csv"
    if include_markouts:
        paths["markouts_jsonl"] = run_dir / "markouts.jsonl"
    return paths


def write_manifest(
    *,
    manifest_path: Path,
    strategy_name: str,
    market_name: str,
    account_name: str,
    account_file: Path,
    profile_file: Path,
    bot_script: str,
    dashboard_script: str,
    bot_config: Dict[str, Any],
    dashboard_config: Dict[str, Any],
) -> None:
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "strategy_name": strategy_name,
        "market_name": market_name,
        "account_name": account_name,
        "account_file": str(account_file),
        "profile_file": str(profile_file),
        "bot_script": bot_script,
        "dashboard_script": dashboard_script,
        "bot_config": bot_config,
        "dashboard_config": dashboard_config,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def run_managed(
    *,
    python_executable: str,
    bot_script: str,
    bot_config: Dict[str, Any],
    dashboard_script: str,
    dashboard_config: Dict[str, Any],
    with_dashboard: bool,
    dry_run: bool,
    title: str,
) -> int:
    bot_cmd = [python_executable, str(SRC / bot_script), *config_to_cli_args(bot_config)]
    dashboard_cmd = [
        python_executable,
        str(SRC / dashboard_script),
        *config_to_cli_args(dashboard_config),
    ]
    child_env = launch_environment(python_executable)

    print(f"{title}")
    print(f"Bot command: {' '.join(bot_cmd)}")
    if with_dashboard:
        print(f"Dashboard command: {' '.join(dashboard_cmd)}")
        print(f"Dashboard URL: http://{dashboard_config.get('host', '127.0.0.1')}:{dashboard_config['port']}")
    print(f"Run directory: {Path(bot_config['events_jsonl']).resolve().parent}")

    if dry_run:
        return 0

    bot_proc = subprocess.Popen(bot_cmd, cwd=str(SRC), env=child_env)
    dash_proc = subprocess.Popen(dashboard_cmd, cwd=str(SRC), env=child_env) if with_dashboard else None
    try:
        while True:
            bot_status = bot_proc.poll()
            dash_status = dash_proc.poll() if dash_proc is not None else None
            if bot_status is not None:
                return bot_status
            if dash_proc is not None and dash_status is not None:
                return dash_status
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping child processes...")
        for proc in (bot_proc, dash_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        for proc in (bot_proc, dash_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.terminate()
        return 0


def base_bot_config(account_config: Dict[str, Any], profile_config: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for key in ("account_balance", "leverage", "api_url", "dex"):
        if key in account_config:
            merged[key] = account_config[key]
    for key, value in profile_config.items():
        if key not in {"market_name", "dashboard_port", "description"}:
            merged[key] = value
    return merged
