"""MemoryMaster dense-recall service launcher (scheduled task MemoryMaster-Dense-Recall)."""
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # the model is cached; never block start-up on the network
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from memorymaster.recall.dense_server import main

# GPU by operator decision 2026-10-06 (host RAM is the scarce resource). Measured the same
# day: GPU 80-322 ms per spaced query (idle P8 clocks), 1.75 GB RSS; CPU 54-109 ms, 2.03 GB RSS.
# 2026-10-30: the model runs in a child process loaded by the first query and ended after 600 s
# without one (--idle-unload-s); the service itself stays near 50 MB. A cold query takes ~6 s.
# Switch back with "--device", "cpu" if latency matters more than the 0.3 GB.
main(["--db", r"{DB}", "--device", "cuda"])
