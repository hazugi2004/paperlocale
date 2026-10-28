"""为真实大图内容流提供局部、有限的解析预算，不改变PDF内容或全局配置。"""
from contextlib import contextmanager
from io import BytesIO

import pypdf
from pypdf.generic import ContentStream


# pypdf 6.21的默认200万token会拒绝正常的密集地图：真实66MB页需约1398万。
# 按该量测最多允许1600万，
# 未知的更大程序仍明确失败，不用无限预算换取表面完成。解压大小、数组、
# 字典、递归及其他原生资源上限全部保留；只调整当前内容流的语法工作量。
MAX_CONTENT_TOKENS = 16_000_000


@contextmanager
def content_parsing_budget(data: bytes):
    """按已解码字节量限制本次解析；正常/异常退出均恢复调用者配置。

    每个语法项至少占用一个输入字节；操作数及其内部对象可能各计一次，
    因此以2×字节量加1给出保守预算，并保留明确上限。它不是放宽科学
    内容、像素或字形校验，也不保证任意畸形/超大PDF可解析。
    pypdf>=6中较早版本没有此配置API，继续其原生解析行为。
    """
    apply = getattr(pypdf, 'apply_configuration', None)
    get = getattr(pypdf, 'get_configuration', None)
    if apply is None or get is None:
        yield
        return
    current = get()
    budget = max(current.stream_parsing_work_maximum_count,
                 min(2 * len(data) + 1, MAX_CONTENT_TOKENS))
    with apply(stream_parsing_work_maximum_count=budget):
        yield


def parsed_content_stream(stream, reader):
    """使用原生语法解析器，保留字符串/注释/内联图片语义，不正则切流。"""
    parsed = ContentStream(stream, reader)
    with content_parsing_budget(parsed.get_data()):
        # operations是惰性属性，必须在局部预算内真正解析；返回后只读
        # 已解析列表，不把放宽的配置泄漏到后续页面或并行任务。
        parsed.operations
    return parsed


def selected_content_operations(stream, reader, operators):
    """完整解析语法，只留调用者实际检查的操作，限制密集图形页的常驻内存。

    不按原始字节搜索关键词；字符串、注释、内联图数据均由原解析器消费。
    返回独立操作列表，包括空列表；不能在恢复默认预算后再触发惰性解析。
    """
    parsed = ContentStream(stream, reader)
    data = parsed.get_data()

    class Selected(list):
        def append(self, operation):
            if operation[1] in operators:
                super().append(operation)

    parsed._operations = Selected()
    with content_parsing_budget(data):
        parsed._parse_content_stream(BytesIO(data))
    return parsed._operations
