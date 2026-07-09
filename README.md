# input-replay-satellite

Human-gated execution of StickerDB production jobs through InputLog.

The satellite watches its lionscliapp-managed inbox, displays pending requests,
and runs them only after the operator explicitly launches the queue from a
prepared Leonardo Design Studio desktop.

## Run

```text
pip install -e .
input-replay-satellite
```

The default local state lives in `.input-replay-satellite/`:

```text
.input-replay-satellite/
  config.json
  inbox/
  runs/
```

StickerDB copies request JSON files into `inbox/`. The request retains absolute
paths to requester-owned inputs, outputs, and its response callback.

Useful commands:

```text
input-replay-satellite list
input-replay-satellite inspect
input-replay-satellite doctor
input-replay-satellite keys
input-replay-satellite get recording.layout
input-replay-satellite set execpath.inputlog-root C:/lion/installed/inputlog
input-replay-satellite set execpath.staging-folder C:/Users/Robert/Launch
input-replay-satellite set execpath.leonardo-save-folder D:/tmp
```

The launch folder is a dedicated transient stage. It must be completely empty
when the operator starts a queue. If it is not empty, the satellite refuses to
run and deletes nothing. Once the blank-folder check passes and the operator
confirms Go, the satellite may clear and reuse that folder until the queue ends.
