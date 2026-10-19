"""Observability failure must not replace a validated business response."""
import logging
from . import monitoring, store
logger = logging.getLogger(__name__)


def record(run_id, state, success, elapsed_ms, warnings, writer=None):
    try:
        (writer or monitoring.record)(run_id, state, success, elapsed_ms)
    except Exception:
        logger.exception('Delivery monitoring write failed: %s', run_id)
        warnings.append('行程结果已保留，但运行监控记录失败，请查看服务日志。')


def log_run(**kwargs):
    try:
        store.log_run(**kwargs)
    except Exception:
        logger.exception('Run metrics write failed: %s', kwargs.get('thread_id'))
