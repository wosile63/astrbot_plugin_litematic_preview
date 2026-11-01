# Litematic 投影预览 · AstrBot 插件

在 QQ 里发送 Minecraft 投影/结构文件并 **@机器人**，自动渲染真实材质的等距 3D 预览图，同时给出尺寸、方块数和所需材料清单。

**当前版本：v1.0.0** · 支持 `.litematic` / `.schem` / `.schematic` / `.nbt` / Axiom `.bp`

当前源码目录：`astrbot_plugin_litematic_preview_v1.0.0/`  
完整配置说明：[`astrbot_plugin_litematic_preview_v1.0.0/README.md`](astrbot_plugin_litematic_preview_v1.0.0/README.md)

## 环境要求

- AstrBot 4.x
- Python 3.10+（AstrBot 自带 venv）
- Java 21+（真实材质渲染；没有 Java 时自动回退简化渲染）
- Python 依赖：Pillow、numpy、openpyxl（`requirements.txt`）

## 安装

1. 把 `astrbot_plugin_litematic_preview_v1.0.0/` 复制到：
   ```
   <AstrBot>/data/plugins/astrbot_plugin_litematic_preview/
   ```
2. 安装依赖：
   ```
   pip install -r requirements.txt
   ```
3. 重启 AstrBot，或在插件管理界面重载插件。
4. 准备原版资源包（见下一节）。

## 官方材质包获取

> ⚠️ 本项目不附带、不分发 Mojang 官方资源。原版资源版权归 Mojang / Microsoft；
> 请确认你自行获取和使用的方式符合 Minecraft EULA 及当地法律。

<!-- 维护者：请在此处填写官方材质包的合法获取方式。 -->

（待维护者填写）

### 资源包放置方式

准备好资源包 zip 后，任选一种：

- 放进 `astrbot_plugin_litematic_preview_v1.0.0/assets/vanilla-resources/`（任意 `.zip`，自动扫描）；
- 放进插件数据目录 `data/plugin_data/astrbot_plugin_litematic_preview/resources/`；
- 在配置 `java_resources_path` 中填写 zip 绝对路径。

没有资源包时 Java 真实材质不可用，会回退到简化渲染（纯色方块）。

## 使用方法

**群聊：**

- 发送投影文件后，在等待时间内 @机器人 触发；
- 或引用/回复文件消息后再 @机器人；
- 配置 `auto_render_group_upload=true` 后，群文件发出即可自动渲染。

**私聊：**

直接发送文件，默认自动渲染。

默认输出 4 个对角视角（2×2 总览图）和材料清单；尺寸、视角、超采样、材料发送方式均可在插件配置中调整。

## 主要配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `render_engine` | `auto` | `auto` / `java` / `python` |
| `java_path` | `""` | Java 路径，留空自动查找 |
| `java_resources_path` | `""` | 原版资源包 zip 路径 |
| `render_unified_size` | `2048` | 统一输出尺寸（256~8192） |
| `render_size_advanced` | 关闭 | 高级单独尺寸 |
| `java_auto_resolution` | `true` | 单视角自动清晰度 |
| `java_auto_pixels_per_block` | `8` | 每方块像素目标 |
| `java_views` | `quad` | 四视角 / 单视角 |
| `java_quad_layout` | `grid` | 总览图 / 4 张独立图 |
| `java_supersampling` | `1` | 超采样 1~4 |
| `java_heap_mb` | `512` | Java 堆下限 |
| `java_render_timeout_seconds` | `60` | 渲染超时下限 |
| `java_material_packs` | `[]` | 材质包列表（自行添加） |
| `material_list_mode` | `image` | image / xlsx / file / text / off |
| `auto_render_group_upload` | `false` | 群文件自动渲染 |
| `auto_render_private` | `true` | 私聊文件自动渲染 |
| `max_schematic_volume` | `32000000` | 包围盒体积上限（格） |

## 材质包（可选）

不内置 XK 材质包；需要时在 `java_material_packs` 中上传/填写自己的材质包路径，并遵守其许可证。

## 注意事项

- 材料统计只统计方块本身，不解析箱子内容/实体；
- 缺材质时回复会列出具体方块名，通常是资源包版本或模组方块不匹配；
- 大投影需要更大 `java_heap_mb` / 超时，或降低尺寸。

## 许可

- 代码采用 MIT License（见 `LICENSE`）。
- 不包含 Mojang 原版资源包和 XK 材质包；获取与使用由用户自行负责。
- Java 渲染器 jar 为 MIT；其他声明见 `THIRD_PARTY_NOTICES.md`。
