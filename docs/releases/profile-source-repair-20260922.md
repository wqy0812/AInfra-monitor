# 请求画像查询修复记录

2026-09-22，测试平台 test1 使用 test4 的 monitoring-api 查询 VM。

## 发现

三个网关抓取 up=1。升级后 DCU 网关最近一小时没有请求，A3 网关有请求且当前指向 DCU Router；画像按网关入口归属，因此不能把 A3 请求改标为 DCU。

跨升级窗口的旧计数器缺少结束边界，汇总按原规则标记 incomplete/null。查询代码把这种局部问题扩展为整窗趋势不可用，导致 DCU/A3 的 6 小时曲线全部为空。此外，sources 使用瞬时查询，遗漏当前已不再输出指标的历史来源。

## 修复及验证

仅修改 profile_counters.py 与 request_profile.py。趋势按问题发生区间屏蔽，保留完整生命周期内的健康曲线；未知汇总仍为 null。sources 改为查询所选时间窗口内的来源。

40 项本地回归测试通过。候选镜像对同一真实 VM 时间窗口进行 1/6/24 小时新旧查询对比：除 sources 与趋势外，values、quality 完全一致；6 小时三条趋势由 0 个有效点恢复为 DCU 各 494、A3 各 522。发布后通过 test1 Web API 再验，随时间窗口移动为 DCU 各 492、A3 各 521，历史 sources 正常返回。跨升级汇总仍为 incomplete/null，未补造数据。本次未进行浏览器渲染验证。

仅替换 test4 monitoring-api，其余运行容器 ID、启动时间和 PID 保持不变；未变更 test1 Web/Engine、网关绑定或推理服务。API 健康正常；A3 推理监控源仍报告 error，不能将此宣称为 A3 模型服务恢复。

镜像：sha256:f806a30dc5e2c4c3c9b2fe24eecaa6c994b1b487739c4d8e3ad30a59953121dd。

回滚容器：monitoring-api-before-profile-source-20260922。

服务器新增目录：test4 `/data2/monitoring/releases/profile-source-20260922/` 及 `before/` 子目录，存放本次脚本、原始源码、镜像构建材料与验收结果。发布脚本为本次专用，不应重复执行。
