#!/usr/bin/env python3
"""从用户自己的 Minecraft 安装目录生成基础原版资源包。

不下载、不再分发 Mojang 资源：只读取本机已经安装/启动过 Minecraft 后下载的
assets index 与 objects，打包成 renderer 需要的资源包 zip。

用法（Windows）：
    python tools/build_vanilla_resource_pack.py --minecraft-dir "%APPDATA%\\.minecraft"
    python tools/build_vanilla_resource_pack.py --version 1.21.9 -o vanilla-resources.zip

生成后：
    - 在插件配置 java_resources_path 里填这个 zip 的绝对路径；或
    - 把 zip 放进插件数据目录 resources/ 下自动发现。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile


def find_version(minecraft_dir: str, version: str | None) -> tuple[str, dict]:
    versions_dir = os.path.join(minecraft_dir, "versions")
    if not os.path.isdir(versions_dir):
        raise SystemExit(f"找不到 versions 目录：{versions_dir}\n请先启动一次 Minecraft 下载资源。")
    if version:
        candidates = [version]
    else:
        candidates = [
            name for name in os.listdir(versions_dir)
            if os.path.isfile(os.path.join(versions_dir, name, f"{name}.json"))
        ]
        candidates.sort(
            key=lambda name: os.path.getmtime(os.path.join(versions_dir, name, f"{name}.json")),
            reverse=True,
        )
    for name in candidates:
        json_path = os.path.join(versions_dir, name, f"{name}.json")
        if not os.path.isfile(json_path):
            if version:
                raise SystemExit(f"找不到版本 JSON：{json_path}")
            continue
        with open(json_path, encoding="utf-8") as f:
            return name, json.load(f)
    raise SystemExit("没有找到可用的 Minecraft 版本 JSON。")


def main() -> int:
    parser = argparse.ArgumentParser(description="从本机 Minecraft 提取原版资源包")
    parser.add_argument(
        "--minecraft-dir",
        default=os.path.join(os.path.expanduser("~"), "AppData", "Roaming", ".minecraft")
        if os.name == "nt" else os.path.join(os.path.expanduser("~"), ".minecraft"),
        help="Minecraft 目录（含 assets/ 与 versions/）",
    )
    parser.add_argument("--version", default="", help="Minecraft 版本，留空自动选择最新安装版本")
    parser.add_argument(
        "-o", "--output", default="vanilla-resources.zip",
        help="输出资源包 zip 路径",
    )
    parser.add_argument("--pack-format", type=int, default=42, help="pack.mcmeta 的 pack_format（默认 42）")
    args = parser.parse_args()

    minecraft_dir = os.path.abspath(os.path.expanduser(args.minecraft_dir))
    version_name, version_json = find_version(minecraft_dir, args.version or None)
    versions = version_json.get("assetIndex") or {}
    index_id = versions.get("id") if isinstance(versions, dict) else None
    if not index_id:
        raise SystemExit(f"版本 JSON 缺少 assetIndex.id：{version_name}")

    index_path = os.path.join(minecraft_dir, "assets", "indexes", f"{index_id}.json")
    if not os.path.isfile(index_path):
        raise SystemExit(
            f"找不到资源索引：{index_path}\n"
            "请先用启动器启动一次该版本，等资源下载完成后再运行本脚本。"
        )
    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)
    objects = index.get("objects")
    if not isinstance(objects, dict):
        raise SystemExit("资源索引格式不正确（缺少 objects）。")

    output = os.path.abspath(os.path.expanduser(args.output))
    parent = os.path.dirname(output)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(output):
        print(f"将覆盖已存在的文件：{output}")

    written = 0
    missing = 0
    total = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "pack.mcmeta",
            json.dumps(
                {
                    "pack": {
                        "pack_format": int(args.pack_format),
                        "description": "Vanilla assets extracted from local Minecraft installation",
                    }
                },
                ensure_ascii=False,
            ),
        )
        for name, meta in objects.items():
            # 只提取 minecraft 命名空间；字体/语言/贴图/模型都够渲染器使用
            if not name.startswith("minecraft/"):
                continue
            total += 1
            hash_value = str((meta or {}).get("hash") or "")
            if len(hash_value) < 2:
                missing += 1
                continue
            obj = os.path.join(minecraft_dir, "assets", "objects", hash_value[:2], hash_value)
            if not os.path.isfile(obj):
                missing += 1
                continue
            zf.write(obj, f"assets/{name}")
            written += 1
    size_mb = os.path.getsize(output) / 1024 / 1024
    print(f"Minecraft 版本：{version_name}（asset index: {index_id}）")
    print(f"写入 {written}/{total} 个 minecraft 资源，缺失 {missing} 个")
    print(f"资源包：{output}（{size_mb:.1f} MB）")
    if written == 0:
        print("没有写入任何资源，请确认 Minecraft 资源已下载完成。", file=sys.stderr)
        return 2
    print("下一步：把该 zip 路径填到插件配置 java_resources_path，或放到插件数据目录 resources/ 下。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
