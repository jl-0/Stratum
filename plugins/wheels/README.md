# plugins/wheels

Drop a built plugin wheel here and `make image` installs it into the worker image, beside the
framework. This is the path for a plugin distribution that lives in **another repository**: it
needs no edit to any file here, which is the whole point.

```bash
cd ../my-lab-plugin && python -m build --wheel
cp dist/my_lab_plugin-0.2.0-py3-none-any.whl <stratum>/plugins/wheels/
cd <stratum> && make image-local
docker run --rm stratum-dev:<tag> stratum plugins list      # your scorer should be listed
```

Wheels are installed with `--no-deps`, so one can never move a version `pixi.lock` decided. The
consequence is that **a dropped wheel's own third-party dependencies must already be in the
environment.** A plugin that needs a new one is not a drop-in: add it under `plugins/` as a path
dependency of this workspace instead, where pixi solves for it and the lock covers it.

A plugin that is a directory here — `plugins/stratum-emit/` is the worked example — needs one line
in the root `pyproject.toml` under `[tool.pixi.pypi-dependencies]` and a `pixi install` to re-lock.
That path is fully reproducible; this one is not, so record the wheel you used.

`*.whl` in this directory is git-ignored.
