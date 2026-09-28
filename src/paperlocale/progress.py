"""供 CLI/App 共同消费的阶段进度；比例是工作阶段权重，不是剩余时间预测。"""
import json


def emit_progress(stage: str, fraction: float, completed: int = 0, total: int = 0):
    print('PAPERLOCALE_PROGRESS '+json.dumps({'stage': stage, 'fraction': max(0., min(1., fraction)),
                                           'completed': completed, 'total': total}, ensure_ascii=False), flush=True)
