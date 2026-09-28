# Project-aware description catalog; legacy functions remain importable.
if __name__ == "__main__":
    from project_coverage import main
    main()
    raise SystemExit(0)

"""Add operator-facing descriptions without changing queries, axes or layouts."""
import copy
import json
import sys
from pathlib import Path
from metric_scope import request_description

BASIC = [
'当前已纳入画像且尚未结束的流式请求数量。它是同时在途数量，不是累计请求数；路由、模型发现、等待下游及向客户端写入等阶段都可能占用在途请求。',
'网关解析确认 stream=true、进入请求画像时累计一次到达。曲线为最近 1 分钟请求计数增量除以时间，即平均到达速率；0.1 请求/秒约等于每分钟 6 个请求。它不代表成功率或生成完成速率。',
'等待画像后台写入的事件队列长度。一个请求可产生多个事件，因此不能把事件数当作请求数。持续增长提示画像写入跟不上事件产生速度。',
'当前内存画像索引条目数，用于近期请求特征分析。条目与业务请求不一定一一对应，也不表示 KV Cache 条目或命中数。',
'画像索引的就绪标志。1 表示距索引重建或重置已满 1 小时，0 表示仍在准备；此标志不保证期间没有画像丢弃或写入错误；它不表示模型推理服务是否可用。',
'最近 1 分钟画像写入错误计数的平均增长速率。统计对象是画像记录的写入错误，不是下游推理失败或 HTTP 错误。',
'最近 1 分钟画像丢弃事件计数的平均增长速率。队列满或事件超过记录大小限制等情况可导致丢弃；它说明画像证据不完整，不等于业务请求被丢弃。',
'当前画像记录占用的存储字节数除以 2^20。表示画像文件空间，不是模型权重、KV Cache 或进程内存大小。'
]
GENERATION = [
'进入下游生成阶段的流式请求结束速率，使用最近 1 分钟结束计数的 rate。包含正常完成、错误、客户端取消、客户端断开和未知结果，不是成功请求速率；与到达速率的差异可能来自在途请求积压或释放。',
'最近 1 分钟网关记录的下游生成错误计数增长速率。范围包含实现所识别的连接、HTTP 及流式协议等错误；每个请求最多记录一个主要错误类别。已经返回 HTTP 200 的流仍可能在后续生成过程中出错。',
'后端错误计数速率 ÷ 全部流式生成结束请求速率 × 100%。分母包含完成、错误、取消、断开和未知结果；不包含入口拒绝、模型发现或探测。',
'结束结果为 client_cancelled 的流式请求速率 ÷ 全部流式生成结束请求速率 × 100%。表示网关观察到请求上下文被取消；仅凭此图不能判定是用户主动操作、上游超时还是其他取消原因。',
'结束结果为 client_disconnected 的流式请求速率 ÷ 全部流式生成结束请求速率 × 100%。通常与网关向客户端写入失败有关，需结合日志定位；不能据此直接判定模型服务失败。',
'结束结果为 unknown 的流式请求速率 ÷ 全部流式生成结束请求速率 × 100%。表示网关没有得到足够证据归入其他结束结果，不能按正常完成解释。'
]
HOST = [
'主机 CPU 忙碌时间占总 CPU 时间的比例。当前查询扣除 idle 和 iowait，再按主机汇总；100% 表示接近全部 CPU 时间处于其余状态。iowait 被扣除，所以该图不能单独排除存储等待。',
'主机内存已用量及总量：已用 = MemTotal − MemAvailable。MemAvailable 包含可回收内存的估算，因此不等于把所有缓存都视作不可用。每条曲线按节点和已用/总量区分。',
'文件系统容量占用比例：(1 − 可用字节/总字节) × 100%。按节点、设备或挂载点分别展示；可用空间采用普通用户可用口径。它是容量百分比，不是磁盘 I/O 忙碌率。',
'设备读/写字节累计计数在最近 1 分钟的平均增长速率，除以 2^20。分别展示读和写及各设备；逻辑盘与其底层物理盘可能重复承载同一 I/O，不应把所有曲线直接相加。',
'网络接口接收/发送字节累计计数在最近 1 分钟的平均增长速率，除以 2^20。单位是字节速率，不是位速率；1 MiB/秒约等于 8.39 Mbit/秒。按节点、网卡及收发方向区分，部分虚拟接口被排除。',
'每张 DCU 卡的设备利用率采样值。按节点和卡号分别展示；不是整个集群的平均值，也不是显存容量使用率。',
'每张 DCU 卡的已用显存字节数除以 2^30。按卡展示绝对容量；要判断显存是否接近耗尽，还需结合该卡总容量。',
'每张 DCU 卡的当前设备温度。按节点和卡号区分；该图未设置统一安全温度阈值，应结合具体设备规格和历史基线判断。',
'每张 DCU 卡的当前功率采样值。瓦特表示瞬时功率，不是累计耗电量；耗电量需要对功率随时间积分。'
]
CACHE = [
'最近约 60 秒内模型前缀缓存命中 Token 占查询输入 Token 的比例。按模型节点/角色区分；属于 Token 加权比例，不是命中请求数占比。',
'最近约 60 秒内设备、主机内存、存储层的缓存命中 Token 分层比例。分母沿用源监控输入 Token 口径；分层语义以源指标为准，不能直接据此推断物理 SSD 读写或全局缓存收益。',
'代表 rank 的 HiCache 已用 Token 容量 ÷ 总 Token 容量 × 100%。这是容量占用率，不是命中率；代表 rank 用于避免直接累加复制容量。',
'代表 rank 的 HiCache 已用及总 Token 容量。单位为可容纳的 Token 数量，不是字节或 Token/秒；不要把复制 rank 的容量相加作为实际可用容量。',
'Mooncake Store 内存后端已使用量及配置总配额，字节除以 2^30。表示 Store 管理的内存容量，不等于整机物理内存已用量。',
'Mooncake Store SSD 后端已使用量及配置总配额，字节除以 2^30。表示后端容量配额；单靠该曲线无法证明已经发生物理 SSD I/O 或配置已经通过读写验收。',
'最近约 60 秒 Store 查询成功计数增量 ÷ 查询计数增量 × 100%。统计的是 Store 查询结果，不等于模型 Token 命中率，也不证明后续数据读取成功。',
'最近约 60 秒 Store memory/ssd 分层查询命中比例，分母使用对应查询计数。它描述 Store 查询层的结果，不代表模型 Token 命中，也不等于物理 SSD I/O 比例。'
]
UNITS = {'%':'百分比；图中 50 表示 50%，不是 0.5。','GiB':'GiB；1 GiB = 2^30 字节。','MiB':'MiB；1 MiB = 2^20 字节。','ms':'毫秒；1000 ms = 1 秒。','s':'秒。','秒':'秒。','Token':'Token 数。','请求':'请求数。','流':'流数量，每条流对应一个在途流式请求。','条目':'条目数。','事件':'事件数。','°C':'摄氏度。','W':'瓦特。','1 = 正常':'状态值；1 = 本次采集成功，0 = 本次采集失败。','1 = 就绪':'状态值；1 = 就绪，0 = 尚未就绪。'}

def meaning(name, key, panel):
    title=panel['spec']['display']['name']
    if name=='gateway': return BASIC[int(key[1:])]
    if name=='gateway-generation':
        if key.startswith('generation-'):return GENERATION[int(key.split('-')[-1])]
        if key.startswith('live-stages-'):
            return '当前流式请求按处理阶段分组计数，一个请求同时只归属一个阶段。阶段包括：路由与模型发现、构造下游请求、等待响应头、等待首个有效输出、读取后续流、读取错误响应、向客户端写入和流结束收尾。阶段切换使曲线变化，不等于请求失败；某阶段持续累积可辅助定位等待位置。'
        if key.startswith('live-idle-') and key!='live-idle-max':
            t=key.split('-')[-1]
            return f'当前已出现有效输出、尚未结束且连续至少 {t} 秒没有新有效内容的流数量。有效内容包含正文、推理、拒绝或工具增量；心跳、空事件和 usage 不重置停顿时钟。阈值为累计包含：≥60 秒的流也计入 ≥30/15/5 秒，四图不能相加。'
        return {
            'live-waiting':'当前已确认为流式、但尚未收到首个有效内容增量的请求数。路由与模型发现等待也包含在内；收到响应头或心跳不算已经产生有效内容。',
            'live-wait-max':'当前仍在等待首个有效内容的请求中，最长的等待时间；从网关接收请求起计时。它是当前最大等待年龄，不是已完成请求 TTFT 的平均值或 P95。',
            'live-idle-max':'按指标范围统计。当前在途状态，5 秒采集、15 秒刷新；采集失败、缺样、过期、升级前或跨进程重启窗口留空。正常空闲显示零。仅累计等待后端有效输出的时间，排除网关写出耗时；首次输出前从读取响应体开始计时。心跳和 usage 不算有效输出。',
            'live-oldest':'按指标范围统计。当前在途状态，5 秒采集、15 秒刷新；采集失败、缺样、过期、升级前或跨进程重启窗口留空。正常空闲显示零。当前一轮写入及刷新持续时间，升高表示输出路径可能存在背压。与后端等待最大值可能来自不同请求。',
            'live-unknown':'当前有效输出观察状态无法可靠判定的流数量，例如解析异常或超出观察限制。这些流退出首输出等待/停顿分类，但仍计入在途年龄和阶段；unknown 不是业务失败的直接结论。',
            'live-nonstream-count':'最近 1 分钟新增非流式请求的估算数量：rate(非流式累计请求数[1m]) × 60。解析确认 stream=false（含省略 stream）时计数。单位是请求数，可能因窗口速率估算出现小数，不是请求/秒。非流式仅在此图统计。'
        }[key]
    if name in ('hosts-dcu','a3-hosts'):return HOST[int(key[1:])]
    if name=='cache-store':return CACHE[int(key[1:])]
    if name=='a3-cache':return [
        'vLLM 实例已分配 KV Cache 的占用比例乘以 100。按实例/角色区分；分母是该实例的 KV Cache 容量，不是整张 NPU 的显存容量。',
        '最近约 60 秒本地前缀缓存命中 Token ÷ 查询 Token × 100%。合并实例时按 Token 加权，不能直接平均各实例百分比。',
        '最近约 60 秒跨实例共享的外部前缀缓存命中 Token ÷ 查询 Token × 100%。这是外部 KV 共享口径，不表示 HiCache CPU、Mooncake Store 或 SSD 命中。'
    ][int(key[1:])]
    if key=='p0':return 'Prometheus 采集目标的抓取状态：1 表示本次指标抓取成功，0 表示失败。图例区分采集 job；采集成功只证明监控端点可读取，不证明模型能够正常完成推理。'
    if 'TTFT' in title or 'ITL' in title or 'E2E' in title:
        typ='TTFT' if 'TTFT' in title else 'ITL' if 'ITL' in title else 'E2E'
        text={'TTFT':'服务侧从请求开始到首 Token 的时延。它不等于客户端看到首字符的完整网络时延，也不同于当前仍在等待请求的最大年龄。','ITL':'服务侧相邻输出 Token 间隔的时延分布。它不是整个回答的生成时间；与按整段输出均摊的 TPOT 也可能不同。','E2E':'服务侧请求从开始到结束的总时延。不同输入/输出长度、缓存状态和并发负载会改变分布；不能把不同阶段的 P95 相减来求网关开销。'}[typ]
        return text+' P50/P95/P99 是窗口内样本分位数，例如 P95 表示约 95% 的观测不超过该值。分位数来自直方图桶估算，不是精确逐请求排序；不能直接平均多节点的分位数。'
    if 'CPU' in title:return '源监控输出的主机 CPU 使用百分比，按节点/角色区分。该图使用派生 CPU 数值；具体系统状态诊断请结合主机页的原始 CPU、磁盘和网络指标。'
    if '运行与排队' in title:return '当前服务侧运行中与排队中的请求数量，按角色及源状态拆线展示。它是时刻值，不是累计请求量；排队持续增加时需结合请求到达速率和完成速率判断。'
    if name=='overview' and key=='p1':return '源服务请求累计计数（sglang:num_requests_total）在连续采样间隔内的平均增长速率。按节点/角色展示；不等于成功请求吞吐，也不等于网关到达速率。同一请求可能经过 P/D 两侧，不能把两侧直接相加当作去重请求量。'
    if '请求速率' in title:return '服务侧已完成请求计数在连续采集间隔内的平均增长速率，按节点/角色展示。完成包含源指标记录的各结束原因，不等于成功量；同一请求可能经过 P/D 两侧，不应把两侧直接相加当作去重请求数。'
    if 'Token' in title:return '服务侧 Token 计数在连续采集间隔内的平均增长速率，按节点/角色展示。Token 是分词器单位，不等同于字符数或请求数；不同模型或输入输出组成的吞吐不宜直接比较。输出 Token 与 Decode Token 遵循各自源指标，源无效时留空。'
    raise ValueError((name,key,title))

def describe(name,key,p):
    unit=p['spec']['plugin']['spec']['yAxis']['label']
    q=' '.join(x['spec']['plugin']['spec']['query'] for x in p['spec']['queries'])
    u=UNITS.get(unit,unit+'；表示每秒的平均变化量。')
    lines=['**指标含义**\n'+meaning(name,key,p),'**Y 轴单位**\n'+u]
    if name=='gateway':
        scope='当前查询显示 DCU 主机网关，且只统计流式请求画像；它不是 DCU/A3 两网关的合计，也不等于下游硬件归属。'
    elif name=='gateway-generation':
        scope='蓝色为 DCU 主机网关，橙色为 A3 主机网关；阶段图的颜色区分处理阶段。按网关所在环境归属，DCU 网关转发至 A3 时仍计入 DCU 曲线。同一请求经两层网关会分别计数，不可相加当作全局去重请求量。'
    else:scope=('A3 环境。' if name.startswith('a3-') else 'DCU 环境。')+'图例区分节点、角色、实例、设备或统计分位数；筛选器仅改变所选序列，不自动生成集群去重汇总。'
    lines.append('**曲线与范围**\n'+scope)
    return request_description('\n\n'.join(lines).replace('**', ''))

def annotate(document):
    d=copy.deepcopy(document)
    for key,p in d['spec']['panels'].items():p['spec']['display']['description']=describe(d['metadata']['name'],key,p)
    return d

if __name__=='__main__':
    source=Path(sys.argv[1]); destination=Path(sys.argv[2])
    docs=json.loads(source.read_text())
    updated=[annotate(d) for d in docs]
    patch={d['metadata']['name']:{k:p['spec']['display']['description'] for k,p in d['spec']['panels'].items()} for d in updated}
    destination.write_text(json.dumps(patch,ensure_ascii=False,indent=2)+'\n')
    print('Annotated',sum(len(p) for p in patch.values()),'panels')
