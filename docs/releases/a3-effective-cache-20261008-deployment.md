# A3 Prefill Token 贡献口径发布

已完成发布，仅作追溯材料；后续发布重新捕获现场版本并使用当前发布工具，不直接重跑本目录。

以现场 API 镜像 `sha256:1f1de688381b4676fd7827c0ead361ab6eccf8f19dec8f808070ccd7f50e1fa0` 为基底，仅覆盖 `monitoring/a3.py`、`monitoring/api.py` 并新增 `monitoring/a3_effective.py`。`source-manifest.json` 记录基底和文件摘要，未替换采集器、VM、Perses 或推理服务。

发布镜像：`sha256:441cb9964954f66e8e12f4e207091791e2ee3d620c51bbe0f0ea72181a6cf8de`。载荷 tar SHA256：`b11c0ee1655f0d1fa56ff857f29ae9f876e82242f5a1eb44ad7472d447b28ea7`。

采用当前 `deploy/replace.py`，失败时保留证据并停止，不回退。独立启用时间为 `1791427375`，既有水位未重置。语义、测试和线上验收见 [发布记录](a3-effective-cache-20261008.md)。原始捕获和载荷为忽略文件；不要提交访问配置或完整容器检查输出。

服务器新增目录：test4 `/data2/monitoring/releases/a3-effective-cache-20261008/` 及其中子目录。
