# Perses 自签 TLS 与 HTTP/2（2026-10-09）

已将 test4 的 Perses 入口切换到 `https://122.247.53.162:18431`。原端口现在只提供 HTTPS，旧 HTTP 链接需要更新；没有新增 HTTP 重定向监听。

容器继续使用 `0.54.0-perf.3`，镜像为 `sha256:978402ac5154e3ee2cf8c66f244fc7169be74125ef01a5fe9d28c1e8a24764e7`。仅替换 Perses 容器并修改其配置，启动参数设置证书、私钥和 TLS 最低版本 1.2，登录 Cookie 设置 Secure。30 张看板、6 个数据源及 3 个项目完整读回与发布前一致。

## 证书

- 自签 RSA 3072，SAN：`IP:122.247.53.162`，用途为 TLS 服务端。
- 有效期：2026-10-09 13:11:39 UTC 至 2027-10-09 13:11:39 UTC。
- SHA-256 指纹：`DE:0D:DE:5D:CD:B0:27:34:70:70:BE:12:D3:D0:BB:9C:B7:8B:6B:79:16:05:BA:72:04:68:B0:3D:B5:99:BB:C8`。
- 服务器证书：`/data2/monitoring/perses/tls/server.crt`；私钥：同目录 `server.key`，root:65532、0640，通过只读挂载交给容器。
- 私钥未下载。公开证书副本：`evidence/perses-tls-20261009/server.crt`。

访问设备需导入并信任公开证书。此次没有修改本机系统信任；浏览器验收仅对该证书公钥使用临时 SPKI 信任。运维 Python 客户端保持证书与 IP 校验，服务器自动加载证书，本机可用 `PERSES_CA_FILE` 指定副本。后续镜像升级保留 TLS 参数和挂载。

## 验证与现场修复

17 项相关回归通过，覆盖可信自签证书、未知证书拒绝、SAN 不匹配拒绝、鉴权、资源读取、数据源选择及升级时 TLS 参数保留。线上 curl 使用证书校验确认 TLS 1.2、TLS 1.3 均协商 HTTP/2，健康接口 200；未登录读取项目返回 401，登录 Cookie 均为 Secure。三个项目的六个数据源代理查询成功。

1080p Chrome 验收 A3 总览真实数据：页面和 18 次查询均为 `h2`，查询错误与浏览器 console error 均为零，无横向溢出。截图和报告在 `output/playwright/perses-tls-20261009/`。未做性能对照，不据此声称具体加速幅度。

首次启动遇到目录受 umask 限制，以及服务器 OpenSSL 默认配置造成 basicConstraints 重复的问题。已修正目录权限，使用显式 OpenSSL 配置和原私钥重新签发证书，再启动当前容器；部署脚本同步修正。启动至最终服务端验收约 141 秒，包含现场修复；没有执行版本回退。

服务器新增目录：

- `/data2/monitoring/perses/tls`
- `/data2/monitoring/perses/evidence/tls-20261009`

服务器证据保存资源前后快照、发布结果、修复说明和运行时文件摘要。运行时已更新 `connection.py`、`project_release.py`、模块安装清单及现行看板链接。其他并行查询优化改动未随本次部署；本次代码尚未提交。
