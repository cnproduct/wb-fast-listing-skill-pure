"""Durable local execution; journal irreversible requests before sending."""
import time
import hashlib
from . import wb
from .pricing import next_discount_at

PENDING = {'/content/v2/cards/upload': 'card_pending',
           '/api/v2/upload/task/club-discount': 'club_pending'}
TRANSIENT = {'wb_unavailable', 'wb_rate_limited'}


def tick(state, token, now=None):
    supplied_now = now
    started = time.monotonic()
    config = state.config()
    if hashlib.sha256(token.encode()).hexdigest() != config['token_fingerprint']:
        raise ValueError('token_changed_bind_again')
    wb.SELLER.set(config['seller_id'])
    for job in sorted(state.jobs(), key=lambda j: j.get("next_run", 0)):
        if time.monotonic() - started > 50:
            break
        now = time.time() if supplied_now is None else supplied_now
        if not job.get('approved') or job['phase'] in ('blocked', 'needs_review', 'prepared') or job.get('next_run', 0) > now:
            continue
        phase = job['phase']
        if phase in ('allocate', 'create') and now - job.get('product', {}).get('captured_at', job['created_at']) > 86400:
            job.update(phase='needs_review', error='capture_expired')
            state.save(job)
            continue
        if phase == 'written':
            phase = 'reprice' if not job.get('reprice_completed') and now >= job['reprice_at'] else 'monitor'
        if phase != job.get('working_phase'):
            job['working_phase'], job['phase_started'] = phase, now
        if phase not in ('written', 'monitor', 'protect') and now - job['phase_started'] > 7200:
            job.update(phase='protect' if job.get('nm_id') and job.get('barcode') else 'needs_review', error='phase_timeout')
            state.save(job)
            continue

        def before_write(method, path):
            pending = PENDING.get(path)
            if path == '/api/v2/upload/task':
                pending = 'reprice_pending' if phase == 'reprice' else 'price_pending'
            if '/stocks/' in path and method == 'PUT' and phase == 'stock':
                pending = 'verify'
            if pending:
                job.update(phase=pending, working_phase=pending, phase_started=now, submission_started_at=now)
                if pending in ('price_pending', 'reprice_pending', 'club_pending'):
                    job.pop('price_task_id', None)
                state.save(job)
        marker = wb.BEFORE_WRITE.set(before_write)
        try:
            result = wb.step(token, job, phase)
            job.update(result)
            job['retries'] = 0
            if result['phase'] != 'needs_review':
                job.pop('error', None)
            if result.get('actual_discount') == 30 and not job.get('first_verified_at'):
                job['first_verified_at'] = now
                job['reprice_at'] = next_discount_at(now)
            job['next_run'] = now + 8
            if job['phase'] == 'written':
                job['next_run'] = min(now + 900, job['reprice_at']) if not job.get('reprice_completed') else now + 900
        except wb.BusinessError as exc:
            code = str(exc)
            job['error'] = code
            if code in ('wb_rate_limited', 'wb_unauthorized', 'wb_permission_denied') and job.get('submission_started_at') == now:
                job['phase'] = phase  # Explicit rejection: retry original request after the wait.
                job.pop('submission_started_at', None)
            job['retries'] = job.get('retries', 0) + 1
            if code in TRANSIENT:
                job['next_run'] = now + max(getattr(exc, 'retry_after', 0), min(300, 15 * 2 ** min(job['retries'], 5)))
            elif code == 'wb_request_rejected' and phase == 'media':
                job.update(phase='relay', relay_index=0, next_run=now+10)
            elif code in ('wb_unauthorized', 'wb_permission_denied'):
                job['next_run'] = now + 900
                state.save(job)
                break  # Same credential: don't spend calls on the rest of the queue.
            elif job['phase'] in ('card_pending', 'price_pending', 'reprice_pending', 'club_pending') and job.get('submission_started_at') == now:
                # A rejected/unknown response can still leave a submitted task. Read back only.
                job['next_run'] = now + 30
            elif phase == 'protect':
                job['next_run'] = now + 60
            else:
                job['phase'] = 'protect' if job.get('nm_id') and job.get('first_verified_at') else 'needs_review'
                job['next_run'] = now + 10
        finally:
            wb.BEFORE_WRITE.reset(marker)
        state.save(job)
    return state.jobs()
