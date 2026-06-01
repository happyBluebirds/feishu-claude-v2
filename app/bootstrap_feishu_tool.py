#!/usr/bin/env python3
"""Bootstrap runner that normalizes Python import paths before loading Feishu tools."""

from __future__ import annotations

import runpy
import site
import sys
from pathlib import Path

INTEGRATION_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """Load one target script after patching sys.path for this machine's Python layout."""

    if len(sys.argv) < 2:
        raise SystemExit("Usage: bootstrap_feishu_tool.py <target-script> [args...]")

    target_script = Path(sys.argv[1]).resolve()
    if not target_script.exists():
        raise SystemExit(f"Target script not found: {target_script}")

    target_parent = str(target_script.parent)
    if target_parent not in sys.path:
        # 脚本整理到 app/hooks/tests 后，runpy 不会自动补目标脚本所在目录；
        # 这里显式加入，保证 hook 之间的本地 import 仍然按整理后的目录解析。
        sys.path.insert(0, target_parent)

    try:
        user_site_packages = site.getusersitepackages()
    except Exception:
        user_site_packages = ""
    if user_site_packages and user_site_packages not in sys.path:
        # 优先使用 requirements 安装到用户级 site-packages 的 SDK，避免本地 vendor 快照遮蔽正式依赖。
        sys.path.append(user_site_packages)

    vendor_site_packages = INTEGRATION_ROOT / "vendor"
    if vendor_site_packages.is_dir():
        vendor_path = str(vendor_site_packages)
        if vendor_path not in sys.path:
            # vendor 仅作为离线兜底放在最后；正常上传源码时不提交该目录。
            sys.path.append(vendor_path)

    sys.argv = [str(target_script), *sys.argv[2:]]
    runpy.run_path(str(target_script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
