"""面向修复台的稳定分类。分类说明不代替原始校验原因，也不放宽内容合同。"""

RULES = {
    'preservation.pixels': ('source.pixels', '保护区像素差异', '查看 PDF 中高亮的实际差异范围；重新分析版面后仍须重新通过保护检查，不能直接忽略该错误。'),
    'preservation.readback': ('source.readback', '译文写入与回读差异', '比较应写入文本和实际回读文本；多出字符可能是另一段重叠，不能仅删除原文来消除报错。'),
    'character': ('content.character', '不可见控制字符', '查看字符码点和上下文；若源文不存在，纠正模型插入的字符；若源文也存在，先核对 PDF 字形，不能猜测科学符号。'),
    'formula': ('content.anchor', '固定公式或引用锚点', '按原文顺序保留全部 {vN} 标记；在 PDF 对照中核对其对应公式或引用，不猜测标记位置。'),
    'restored_content': ('content.anchor', '锚点及还原后的句子结构', '核对锚点顺序和还原后的括号；编辑失败译文时不要重复添加锚点已经包含的括号。'),
    'number': ('content.number', '数字保留', '逐项对照缺失数字和原句；核对年代、阈值、精度及正负号后修改译文。'),
    'quantity': ('content.quantity', '数值与单位配对', '保持数值与对应单位相邻且含义一致；单位不能串到另一处数字上，也不能自行换算。'),
    'unit': ('content.quantity', '单位保留', '对照源 PDF 核对单位及上下标，保留数值和单位的对应关系。'),
    'abbreviation': ('content.abbreviation', '缩写保留', '保留指出的缩写及出现次数；即使展开为中文全称，仍需在对应位置保留原缩写。'),
    'scientific_literal': ('content.expression', '科学表达式', '逐字核对表达式中的字母、运算符、小数点及下标，不能凭语义猜补。'),
    'style': ('content.structure', '样式标记顺序', '保持原文样式标记成对且顺序一致，只编辑标记之间的译文。'),
    'url': ('content.identifier', '网址保留', '将原文网址完整保留，勿翻译、断词或改变链接字符。'),
    'doi': ('content.identifier', 'DOI 保留', '逐字保留原文 DOI。'),
    '领域术语缺失': ('content.terminology', '领域术语', '按提示使用当前领域包指定的术语，保留原文科学含义。'),
    '长正文片段缺少中文译文': ('content.language', '缺少中文正文', '输入完整中文译文；若实际为无需翻译的作者或出版信息，核对 PDF 后明确保留原文。'),
    '译文为空': ('content.empty', '空译文', '填写完整译文并保留全部固定标记。'),
}


def rule_detail(message: str) -> dict:
    if '字体缺少字形' in message:
        return {'code': 'layout.font', 'title': '字体字形覆盖', 'message': message,
                'guidance': '查看原 PDF 中的真实符号；若属于原生公式，使用重新分析版面；若是译文字符，选择覆盖该字符的字体或纠正错误输入。不能猜换数学符号。'}
    if '无法放入段落框' in message:
        return {'code': 'layout.capacity', 'title': '段落容量', 'message': message,
                'guidance': '对照高亮区域核对标题和正文是否误合并；可重新分析版面，或等义精炼失败译文，保存后再次检查实际排版。'}
    key = next((key for key in RULES if message.startswith(key)), None)
    code, title, guidance = RULES.get(key, ('content.other', '内容校验', '结合原文、失败译文和具体规则修订；校验通过后仍会检查实际排版。'))
    return {'code': code, 'title': title, 'message': message, 'guidance': guidance}


def classify(error: Exception, category: str) -> dict:
    message = str(error).lower()
    if category == 'translation':
        values = ('content.validation', '译文内容校验', '可在本窗口编辑失败译文并校验；合格片段继续复用。')
    elif category == 'layout' and '字体缺少字形' in message:
        values = ('layout.font', '中文字体缺字', '核对缺少的具体字符，纠正译文控制字符或在应用设置选择包含该字形的中文字体；缩小字号无效。')
    elif category == 'layout':
        values = ('layout.capacity', '译文排版容量', '先核对 PDF 区域，再等义修订译文或降低该片段字号；不能删减科学信息。')
    elif category in {'extraction', 'source-glyph', 'preservation'}:
        values = ('source.'+category, '源 PDF 提取或保护', '在 PDF 预览核对报错区域；可用当前支持的保留原文选项继续，但结果将明确标为部分翻译。未知结构需修复引擎，不能绕过保护。')
    elif any(t in message for t in ('版面计划', '旧计划', '回读译文', '像素改变')):
        values = ('source.plan' if '计划' in message else 'source.preservation', '版面计划或输出一致性', '核对原页和具体区域；提取规则更新后可在应用内重新分析并导入合格缓存。无法证明内容完整时保留断点，不能绕过保护。')
    elif isinstance(error, PermissionError):
        values = ('file.permission', '路径不可写或不可读', '在应用内更换可写工作区或保存位置，或选择可读取的 PDF。')
    elif isinstance(error, FileExistsError):
        values = ('file.output_conflict', '输出文件冲突', '在应用内选择新的保存文件名；不覆盖来源不明的已有文件。')
    elif isinstance(error, FileNotFoundError):
        values = ('environment.missing', '文件或依赖缺失', '核对所示路径；在应用设置中重新选择 PDF、CLI 或中文字体后继续。')
    elif category == 'provider' or any(t in message for t in ('401', '403', '429', 'timeout', 'timed out', 'connection', 'api 密钥', 'provider', 'codex', '模型')):
        if any(t in message for t in ('401', '403', 'api 密钥', 'unauthorized', 'authentication')):
            values = ('service.authentication', '服务授权', '在应用设置更新 API 密钥或完成服务登录，再从断点继续。')
        elif any(t in message for t in ('429', 'quota', 'rate limit', '额度')):
            values = ('service.quota', '服务额度或限流', '检查服务额度，等待恢复后再重试；反复立即重试不会修复额度问题。')
        elif any(t in message for t in ('短键', 'json', '顺序', 'schema')):
            values = ('service.response', '模型响应格式', '从断点重试；若仍失败，减小请求批次。原有合格译文保留。')
        else:
            values = ('service.connection', '模型连接或配置', '在应用设置核对服务地址、模型和运行环境，再重试；不要在原断点混用不同模型。')
    else:
        values = ('operation.other', '其他运行错误', '保留诊断和断点，核对下方原因。不能安全自动修复的未知错误不会被标为成功。')
    return dict(zip(('code', 'title', 'guidance'), values))
