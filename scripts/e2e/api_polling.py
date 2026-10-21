"""Small client helper that resolves operation 202 responses to final results."""
import time


def request_and_wait(client, method, path, *, json=None, headers=None, timeout_seconds=1200):
    """Return (final_response, operation_id, accepted_response).

    Synchronous responses and business-level clarification 202 responses are
    returned unchanged. Only a 202 carrying an operation_id is polled.
    """
    response = client.request(method, path, json=json, headers=headers)
    if response.status_code != 202:
        return response, None, None
    try:
        accepted = response.json()
    except ValueError:
        return response, None, None
    operation_id = accepted.get('operation_id')
    if not operation_id:
        return response, None, None
    status_url = accepted.get('status_url') or f'/api/v1/operations/{operation_id}'
    result_url = accepted.get('result_url') or f'/api/v1/operations/{operation_id}/result'
    accepted_response = response
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        retry_after = response.headers.get('Retry-After', '3')
        try:
            pause = min(5.0, max(0.25, float(retry_after)))
        except (TypeError, ValueError):
            pause = 1.0
        time.sleep(min(pause, max(0.0, deadline - time.monotonic())))
        # The submission credentials must accompany every operation read;
        # supplying them only to the initial POST leaves polling unauthenticated.
        status = client.get(status_url, headers=headers)
        if status.status_code != 200:
            if status.status_code in {429, 502, 503, 504}:
                continue
            status.raise_for_status()
        state = status.json().get('status')
        if state in {'COMPLETED', 'FAILED'}:
            result = client.get(result_url, headers=headers)
            if result.status_code == 202:
                response = result
                continue
            return result, operation_id, accepted_response
        response = status
    raise TimeoutError(f'workflow operation {operation_id} did not finish before the polling deadline')
