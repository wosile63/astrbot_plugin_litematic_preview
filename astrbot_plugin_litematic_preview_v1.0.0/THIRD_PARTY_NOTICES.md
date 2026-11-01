# 第三方组件与资源声明

本项目的**代码**采用 MIT 许可（见 `LICENSE`）。以下第三方组件/资源仍遵循各自许可，
MIT 许可不覆盖它们。

## 1. Java 渲染器

- 文件：`assets/standalone-renderer/litematic-standalone-renderer-0.2.8.jar`
- 来源：[halfkite/koishi-plugin-litematic-renderer](https://github.com/halfkite/koishi-plugin-litematic-renderer)
- 许可：MIT，见 `assets/LICENSE.koishi-plugin-litematic-renderer`
- 说明：本项目按 MIT 许可对 jar 的分辨率上限做了 4096→8192 的兼容补丁。

## 2. Minecraft 原版资源包

- **不随本发布版分发**。
- 用户需要从自己的 Minecraft 安装生成/准备资源包，并通过插件配置
  `java_resources_path` 或插件数据目录 `resources/` 提供。
- Minecraft 原版资源版权归 Mojang / Microsoft；
  请仅在自己合法拥有的 Minecraft 范围内使用，不要公开再分发。

## 3. 可选材质包（例如 XK）

- **不随本发布版分发**。
- 用户自行添加材质包时需遵守其作者声明的许可证。
- 例如 `XK redstone display 26.3.0`（作者 Xe_Kr）采用
  CC BY-NC-ND 4.0（署名-非商业性使用-禁止演绎），需保留作者署名并禁止商业使用。

## 4. 材料清单数据

- 方块/物品/堆叠数据来自 MIT 项目
  [PrismarineJS/minecraft-data](https://github.com/PrismarineJS/minecraft-data)。
- 中文名来自 Mojang 官方语言文件（本地个人使用），不在本项目再授权。
- 详见 `assets/materials_data.LICENSE.txt`。

## 5. 项目图标

- 图标为本项目原创几何图形，代码/源文件可随项目 MIT 许可使用。
