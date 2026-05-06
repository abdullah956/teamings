# Extension gotchas

Things that will bite you when extending this harness. Read before
adding a new target adapter or changing how results are aggregated.

## Adding a new target adapter

Concretely, you need to change three places:

1. Write `targets/<provider>_target.py` subclassing `Target` with
   `provider = "..."` and a real `query()`.

2. Register it in `runner.py`'s `build_target_registry` — there's a
   hardcoded `if/elif` chain. Forgetting to add a branch means your
   `--targets foo` raises `unknown target: 'foo'`.

3. Make sure `provider/model_name` in `target.name` is filesystem-safe
   AND unique per (provider, model). The summary pivot in
   `render_summary` groups by `target` (the `name` string), so two
   adapters that collide on `name` will silently merge their results
   into the same column — you won't get an error, just wrong
   percentages.

The non-obvious one is #3: nothing crashes, the CSV looks fine, and
the table renders. The bug only shows up when you cross-check fail
counts against what you expected. The fix when you eventually hit it
is to give each adapter a distinct `model_name` even if they're
calling the same underlying model via different routes.
