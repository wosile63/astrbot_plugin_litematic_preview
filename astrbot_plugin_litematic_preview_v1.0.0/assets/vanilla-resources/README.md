# assets/vanilla-resources

把你的 **Minecraft 原版资源包 zip** 放在这个文件夹里即可，插件会自动扫描并用于 Java 真实材质渲染。

- 支持任意 `.zip` 文件名；推荐命名为 `Minecraft-26.2-Vanilla-Resources.zip`；
- 也可以改放到插件数据目录 `resources/`，或在配置 `java_resources_path` 里填写绝对路径；
- 官方资源包需要你自己从 Minecraft 安装生成/准备（`tools/build_vanilla_resource_pack.py`），
  本项目**不附带** Mojang 原版资源，请勿把官方资源提交到公开仓库；
- 仓库的 `.gitignore` 已忽略本目录下的 `.zip`，避免误提交。

如果同时存在多个资源包，插件优先使用文件名含 `vanilla` / `原版` / `resources` 的包，
然后按文件名排序；配置路径和插件数据目录 `resources/` 的优先级高于本文件夹。
