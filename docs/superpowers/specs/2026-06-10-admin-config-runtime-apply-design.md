# Admin Config Runtime Apply Design

## Goal

Implement the first real Admin Config apply backend for the runtime-safe option
`staged_recovery.max_parallel_starts_per_host`.

## Scope

The feature applies only drafts whose changes are fully runtime-safe. In the
current schema this means only:

```text
staged_recovery.max_parallel_starts_per_host
```

When such a draft is applied, Masakari API writes the value to the existing
staged recovery etcd runtime override used by `EtcdStartLimiter`.

Non-runtime changes still produce `reconfigure_required` plan steps. Applying a
draft that includes non-runtime changes must not write partial runtime state; it
is rejected with `409 Conflict` because `masakari.conf` values are immutable
through Masakari API.

## API Behavior

`POST /v1/admin-config-drafts/{draft_id}/apply` keeps the existing response
shape. For runtime-only drafts:

```json
{
  "apply_job": {
    "status": "succeeded",
    "result": {
      "backend": "runtime_etcd",
      "changed": true,
      "runtime_updates": [
        {
          "group": "staged_recovery",
          "option": "max_parallel_starts_per_host",
          "value": 4,
          "source": "runtime"
        }
      ]
    }
  }
}
```

If the draft does not contain runtime updates, or contains any
`reconfigure_required` step, apply is rejected before an apply job is created:

```text
409 Conflict
Draft contains immutable Masakari configuration changes. Only runtime configuration changes are supported by Masakari API apply.
```

`GET /v1/admin-config/effective` should expose effective source metadata for the
runtime mutable option so Horizon can show whether the value comes from
`masakari.conf` or from etcd runtime override:

```json
{
  "max_parallel_starts_per_host": {
    "value": 4,
    "source": "runtime"
  }
}
```

Non-runtime and secret-like options keep the existing primitive or masked
representation.

## Error Handling

Validation remains the primary guard. If etcd rejects the value through
`InvalidInput`, the apply request returns `400 Bad Request`, the job is updated
to `failed`, and no draft is marked `applied`.

If the etcd backend is unavailable, the apply request returns `500` through the
existing Masakari exception path and the job is updated to `failed` with the
error string.

For non-runtime or mixed drafts, no apply job is created. The draft keeps its
current status, and the latest validation/plan is stored for Horizon to display.

## Testing

Tests must patch `EtcdStagedRecoveryStore` at the Admin Config controller
boundary and verify:

- runtime-only apply calls `set_max_parallel_starts_per_host`;
- result backend is `runtime_etcd`;
- effective config reports runtime source/value;
- mixed runtime + reconfigure draft does not call runtime apply and is rejected
  with `409 Conflict`;
- existing route tests still pass.
